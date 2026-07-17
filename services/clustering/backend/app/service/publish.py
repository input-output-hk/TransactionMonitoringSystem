"""Publish the engine's per-transaction verdicts as contract_anomaly rows the
host TMS reads.

The host surfaces a watched contract's anomalous transactions as the synthetic
``contract_anomaly`` attack class by reading ``tx_contract_anomaly`` (in the
engine database ``tms_clustering``) cross-server. This module fills that table
from the engine's own resolved verdicts. Two sources, because the engine stores
batch and incremental verdicts differently:

- Batch fit: a transaction in a cluster run is "classified in batch"; its
  verdict is resolved on read from the cluster run + the paired anomaly run
  (auto-anomaly when ``votes >= FLAG_VOTE_THRESHOLD``, malicious/benign when its
  cluster carries a propagating label). We resolve it here with the same
  ``compute_verdicts`` the UI reads use, then publish the flagged txs with their
  consensus/iso/lof from the anomaly run.
- Online classify: incrementally-scored new txs land in ``tx_classifications``
  with a resolved verdict already; we copy the flagged ones straight across,
  minus any a human has labeled benign (their explicit/cluster label overrides
  the model verdict).

Only ``malicious`` / ``anomaly`` verdicts are published (``normal`` carries no
signal; ``benign`` is a human "cleared" label that must not raise a host band).

The projection is AUTHORITATIVE, not append-only: every publish reconciles the
table for ``(network, target, feature_set)`` to exactly the currently-flagged
set. A tx that
was flagged before but is now benign/normal (re-fit reclassified it, a human
labeled its cluster benign, or a label was applied/cleared) is RETRACTED by
appending a superseding ``normal`` tombstone, so the host stops surfacing a
stale Contract Anomaly. ``tx_contract_anomaly`` is
``ReplacingMergeTree(published_at)`` keyed by (network, tx_hash, target); the
host reads FINAL, so the row with the newest ``published_at`` (real verdict or
tombstone) wins. Every reconciliation stamps one monotonic ``published_at`` on
all the rows it writes (tombstones included), so the LATEST reconciliation
always wins — even when it re-publishes a positive whose SOURCE time
(``scored_at``, from the original run/classify) is older than a prior tombstone.
Re-publishing the same verdicts is therefore idempotent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.service.verdicts import (
    VERDICT_ANOMALY,
    VERDICT_MALICIOUS,
    VERDICT_NORMAL,
    _anomaly_votes,
    _resolve_verdicts,
    _run_membership,
)
from app.storage.protocol import Repo

logger = logging.getLogger(__name__)


def _host_known_only(repo: Repo, target: str, hashes: set[str]) -> set[str]:
    """``hashes`` intersected with the transactions the HOST actually knows.

    The host's notifications poller alerts on EVERY flagged
    tx_contract_anomaly row with no host-membership check and links to
    /attacks/{tx_hash}, so publishing a hash the host never ingested (a
    backfilled-history tx living only in the engine's own tables) would page
    an operator to a page the host cannot render. Intersecting with HOST
    membership is exact by construction, regardless of what the engine's local
    tables contain: a host-known tx is never suppressed (recall is preserved
    even against stale local rows left by an earlier blockfrost-primary run on
    the same database), and a host-unknown tx is never published, even if the
    HISTORY_SOURCE setting changed after its backfill. Pure host_ch and kupo
    deployments pass through unchanged (every classified tx is a host row).
    History txs keep their full verdicts INSIDE the module (the Validators UI
    reads the module's own runs/classifications); only the host-facing
    projection is bounded."""
    if not hashes:
        return hashes
    return hashes & repo.host_known_tx_hashes(target, hashes)


# Verdicts that constitute the contract_anomaly attack surface (see module doc).
_PUBLISHED = (VERDICT_MALICIOUS, VERDICT_ANOMALY)

_COLUMNS = [
    "network",
    "tx_hash",
    "target",
    "cluster_id",
    "iso_score",
    "lof_score",
    "consensus",
    "votes",
    "verdict",
    "model_id",
    "feature_set",
    "evidence",
    "scored_at",
    "published_at",
]


def _as_datetime(value: Any) -> datetime:
    """Coerce a ClickHouse-returned timestamp (datetime or 'YYYY-MM-DD HH:MM:SS'
    string) to a datetime for use as the ReplacingMergeTree version stamp."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


# Smallest step the published_at column can represent: it is DateTime64(6),
# microsecond precision (009_contract_anomaly.sql), so +1us is the minimal
# strictly-greater version when now() has fallen behind the table MAX.
_VERSION_EPSILON = timedelta(microseconds=1)


def _reconciliation_version(repo: Repo, target: str, network: str) -> datetime:
    """Monotonic ``published_at`` for one reconciliation pass.

    Wall-clock ``now()`` alone is not monotonic: after a backward clock step
    (NTP correction, VM migration, host reboot) a new pass would stamp a version
    BELOW rows already published, so everything it writes, re-raised real
    verdicts included, would lose to a stale ``normal`` tombstone on FINAL and
    the host would keep suppressing a live alert. Guard by never stamping at or
    below the table's current MAX for (network, target): when ``now()`` trails
    it, step one epsilon past the MAX instead.

    Scoped to (network, target), not feature_set: the table's replacing key is
    (network, tx_hash, target), so versions must be comparable across every
    feature set that can write rows for the same key.
    """
    db = repo._db  # type: ignore[attr-defined]
    rows = repo.client.query(  # type: ignore[attr-defined]
        "SELECT max(published_at) FROM " + db + ".tx_contract_anomaly "
        "WHERE network = {net:String} AND target = {tgt:String}",
        parameters={"net": network, "tgt": target},
    ).result_rows
    now = datetime.now(UTC).replace(tzinfo=None)
    # max() over zero rows returns the column type's zero value (1970-01-01),
    # which is always below now(), so a never-published target needs no special
    # case beyond the None guard for drivers that surface NULL instead.
    current = _as_datetime(rows[0][0]) if rows and rows[0][0] is not None else None
    if current is not None and current >= now:
        return current + _VERSION_EPSILON
    return now


def _publish_batch(
    repo: Repo,
    target: str,
    network: str,
    feature_set: str,
    published_at: datetime,
) -> set[str]:
    """Resolve and publish the latest batch fit's flagged verdicts. Returns the
    set of tx_hashes published (empty if the target has no cluster run yet).

    Resolution is label-aware (``_resolve_verdicts`` folds in the target's manual
    labels), so a tx whose cluster was labeled benign resolves to ``benign`` and
    is excluded here, which lets the reconciliation step retract any stale row.
    ``published_at`` is the reconciliation version stamped on every row (see the
    table doc); ``scored_at`` stays the source run time."""
    cluster_of, run = _run_membership(repo, target, feature_set)
    if not run or not cluster_of:
        return set()
    near = run["created_at"]
    votes = _anomaly_votes(repo, target, feature_set, near=near)
    tx_verdict, _ = _resolve_verdicts(repo, target, cluster_of, votes)
    flagged = {tx: v for tx, v in tx_verdict.items() if v in _PUBLISHED}
    kept = _host_known_only(repo, target, set(flagged))
    flagged = {tx: v for tx, v in flagged.items() if tx in kept}
    if not flagged:
        return set()

    anomaly_run_id = repo.latest_anomaly_run(target, feature_set, near=near)
    scores: dict[str, tuple[float, float, float]] = {}
    if anomaly_run_id:
        db = repo._db  # type: ignore[attr-defined]
        for r in repo.client.query(  # type: ignore[attr-defined]
            f"SELECT toString(tx_hash), iso_score, lof_score, consensus "
            f"FROM {db}.anomaly_scores FINAL WHERE run_id = {{r:String}}",
            parameters={"r": anomaly_run_id},
        ).result_rows:
            scores[str(r[0])] = (float(r[1]), float(r[2]), float(r[3]))

    stamp = _as_datetime(near)
    model_id = anomaly_run_id or run["run_id"]
    rows = []
    for tx, verdict in flagged.items():
        iso, lof, consensus = scores.get(tx, (float("nan"), float("nan"), 0.0))
        rows.append(
            [
                network,
                tx,
                target,
                int(cluster_of.get(tx, -1)),
                iso,
                lof,
                consensus,
                int(votes.get(tx, 0)),
                verdict,
                model_id,
                feature_set,
                "{}",
                stamp,
                published_at,
            ]
        )
    db = repo._db  # type: ignore[attr-defined]
    repo.client.insert(  # type: ignore[attr-defined]
        f"{db}.tx_contract_anomaly",
        rows,
        column_names=_COLUMNS,
    )
    return set(flagged)


def _publish_online(
    repo: Repo,
    target: str,
    network: str,
    feature_set: str,
    published_at: datetime,
) -> set[str]:
    """Copy the target's flagged ``tx_classifications`` (incrementally-scored new
    txs) into ``tx_contract_anomaly``, in-database (both in ``tms_clustering``).
    Returns the set of tx_hashes published.

    Label resolution (``tx_labels`` FINAL + ``deleted = 0``, so a CLEARED label
    stops applying):
    - benign-labeled txs are EXCLUDED (the human "cleared" verdict overrides the
      model), so the reconciliation step can retract a stale alert;
    - malicious-labeled txs are INCLUDED and published as ``malicious`` even when
      the model verdict was ``normal`` — this covers single-tx / noise / new-tx
      judgements (the ``label_transaction`` endpoint) that the cluster path can't
      reach. (A malicious label on a tx with no ``tx_classifications`` row, i.e.
      never online-scored, still can't be published here — rare; the batch path
      covers cluster members.)

    ``published_at`` is the reconciliation version (see the table doc)."""
    db = repo._db  # type: ignore[attr-defined]
    verdicts = ", ".join(f"'{v}'" for v in _PUBLISHED)
    params = {"net": network, "tgt": target, "fs": feature_set, "pub": published_at}
    # Built by concatenation (not an f-string) so the {name:Type} server-binding
    # placeholders stay literal while db / verdicts interpolate. Active-label
    # subqueries: FINAL + deleted=0 means a cleared label no longer applies.
    benign_sub = (
        "(SELECT toString(tx_hash) FROM " + db + ".tx_labels FINAL "
        "WHERE target = {tgt:String} AND deleted = 0 AND label = 'benign')"
    )
    malicious_sub = (
        "(SELECT toString(tx_hash) FROM " + db + ".tx_labels FINAL "
        "WHERE target = {tgt:String} AND deleted = 0 AND label = 'malicious')"
    )
    where = (
        "target = {tgt:String} AND feature_set = {fs:String} AND ("
        "(verdict IN (" + verdicts + ") AND toString(tx_hash) NOT IN " + benign_sub + ")"
        " OR toString(tx_hash) IN " + malicious_sub + ")"
    )
    published = {
        str(r[0])
        for r in repo.client.query(  # type: ignore[attr-defined]
            "SELECT toString(tx_hash) FROM " + db + ".tx_classifications FINAL WHERE " + where,
            parameters=params,
        ).result_rows
    }
    published = _host_known_only(repo, target, published)
    if not published:
        return set()
    # A human malicious label overrides the stored model verdict on publish.
    verdict_expr = "if(toString(tx_hash) IN " + malicious_sub + ", 'malicious', toString(verdict))"
    # The INSERT is pinned to the precomputed kept set (not just the WHERE):
    # this applies the history filter in-database AND closes the race where a
    # row lands between the SELECT above and this INSERT — the returned set and
    # the written rows must be the same set, or _retract_stale's keep drifts.
    params["keep"] = sorted(published)
    repo.client.command(  # type: ignore[attr-defined]
        "INSERT INTO " + db + ".tx_contract_anomaly (" + ", ".join(_COLUMNS) + ") "
        "SELECT {net:String} AS network, toString(tx_hash), target, cluster_id, "
        "iso_score, lof_score, consensus, votes, " + verdict_expr + ", model_id, "
        "toString(feature_set), '{}' AS evidence, toDateTime(scored_at), "
        "{pub:DateTime64(6)} AS published_at "
        "FROM "
        + db
        + ".tx_classifications FINAL WHERE "
        + where
        + " AND toString(tx_hash) IN {keep:Array(String)}",
        parameters=params,
    )
    return published


def _retract_stale(
    repo: Repo,
    target: str,
    network: str,
    feature_set: str,
    *,
    keep: set[str],
    published_at: datetime,
) -> int:
    """Append a ``normal`` tombstone for every currently-published (non-normal) tx
    of this ``(network, target, feature_set)`` that is NOT in ``keep`` (the
    freshly-published flagged set), so the host stops surfacing a now-benign/
    normal transaction.

    Scoped to ``feature_set``: a reconciliation only retracts rows its own
    feature set published (the FINAL-winning row must carry it), so a second
    publish path can never tombstone another feature set's live verdicts just
    because its own pass did not re-flag them.

    Returns the number of rows retracted. The tombstone carries this
    reconciliation's ``published_at`` version, so it supersedes the stale row on
    FINAL, while any later reconciliation (a re-flag after a label is cleared, or
    a future fit) gets a still-newer ``published_at`` and supersedes the tombstone
    in turn. ``scored_at`` is set to ``published_at`` too (a tombstone has no
    source verdict time of its own)."""
    db = repo._db  # type: ignore[attr-defined]
    current = {
        str(r[0])
        for r in repo.client.query(  # type: ignore[attr-defined]
            "SELECT DISTINCT toString(tx_hash) FROM " + db + ".tx_contract_anomaly "
            "FINAL WHERE network = {net:String} AND target = {tgt:String} "
            "AND feature_set = {fs:String} AND verdict != {normal:String}",
            parameters={"net": network, "tgt": target, "fs": feature_set, "normal": VERDICT_NORMAL},
        ).result_rows
    }
    stale = current - keep
    if not stale:
        return 0
    rows = [
        [
            network,
            tx,
            target,
            -1,
            float("nan"),
            float("nan"),
            0.0,
            0,
            VERDICT_NORMAL,
            "",
            feature_set,
            "{}",
            published_at,
            published_at,
        ]
        for tx in stale
    ]
    repo.client.insert(  # type: ignore[attr-defined]
        f"{db}.tx_contract_anomaly",
        rows,
        column_names=_COLUMNS,
    )
    return len(rows)


def _publish_labels(
    repo: Repo,
    target: str,
    network: str,
    feature_set: str,
    published_at: datetime,
    *,
    exclude: set[str],
) -> set[str]:
    """Publish malicious MANUAL labels that neither the online nor batch path can
    reach, and return ALL malicious-labeled hashes for this target.

    The online path reads ``tx_classifications`` and the batch path reads cluster
    membership, so a tx that was directly labeled malicious (``label_transaction``)
    but was never online-scored AND is not a cluster member is published by
    neither (the module's own docstring conceded this gap). Without this it is
    silently never surfaced as a contract_anomaly, and ``_retract_stale`` would
    even tombstone it if a prior pass had published it.

    Source-agnostic: reads ``tx_labels`` directly (FINAL + deleted = 0, so a
    cleared label no longer applies). Inserts a synthesized malicious row (no
    cluster/score data of its own) for any hash not already published this pass;
    returns the full labeled set so the caller folds it into ``keep``."""
    db = repo._db  # type: ignore[attr-defined]
    labeled = {
        str(r[0])
        for r in repo.client.query(  # type: ignore[attr-defined]
            "SELECT DISTINCT toString(tx_hash) FROM " + db + ".tx_labels FINAL "
            "WHERE target = {tgt:String} AND deleted = 0 AND label = {mal:String}",
            parameters={"tgt": target, "mal": VERDICT_MALICIOUS},
        ).result_rows
    }
    # The INSERT is host-membership-bounded, but the RETURN value stays
    # unfiltered: it feeds _retract_stale's keep set, and a host-unknown
    # labeled tx was never published, so keeping it in keep is a harmless
    # no-op. Unfiltered is the simpler contract ("all malicious-labeled
    # hashes").
    fresh = _host_known_only(repo, target, labeled - exclude)
    if fresh:
        rows = [
            [
                network,
                tx,
                target,
                -1,
                float("nan"),
                float("nan"),
                0.0,
                0,
                VERDICT_MALICIOUS,
                "",
                feature_set,
                "{}",
                published_at,
                published_at,
            ]
            for tx in fresh
        ]
        repo.client.insert(  # type: ignore[attr-defined]
            f"{db}.tx_contract_anomaly",
            rows,
            column_names=_COLUMNS,
        )
    return labeled


def publish_contract_anomaly(
    repo: Repo,
    target: str,
    *,
    network: str,
    feature_set: str = "shape",
) -> int:
    """Reconcile the host-visible contract_anomaly projection for a target.

    Publishes all currently-flagged verdicts (batch fit + online classify) and
    retracts any previously-published tx that is no longer flagged. Idempotent.
    Returns the number of flagged (non-tombstone) contract_anomaly rows now
    present for (network, target, feature_set) so callers can log coverage."""
    # One monotonic reconciliation version for every row written this pass: a
    # later publish (incl. a re-raise after a benign label is cleared) always
    # gets a newer published_at and wins on FINAL, regardless of source times
    # and even across a backward wall-clock step (see _reconciliation_version).
    published_at = _reconciliation_version(repo, target, network)
    online_flagged = _publish_online(repo, target, network, feature_set, published_at)
    batch_flagged = _publish_batch(repo, target, network, feature_set, published_at)
    # Malicious manual labels the online/batch paths can't reach (never-scored,
    # non-cluster txs). Folded into keep so they are not tombstoned.
    label_flagged = _publish_labels(
        repo,
        target,
        network,
        feature_set,
        published_at,
        exclude=online_flagged | batch_flagged,
    )
    retracted = _retract_stale(
        repo,
        target,
        network,
        feature_set,
        keep=online_flagged | batch_flagged | label_flagged,
        published_at=published_at,
    )
    db = repo._db  # type: ignore[attr-defined]
    rows = repo.client.query(  # type: ignore[attr-defined]
        f"SELECT count() FROM {db}.tx_contract_anomaly FINAL "
        "WHERE network = {net:String} AND target = {tgt:String} "
        "AND feature_set = {fs:String} AND verdict != {normal:String}",
        parameters={"net": network, "tgt": target, "fs": feature_set, "normal": VERDICT_NORMAL},
    ).result_rows
    n = int(rows[0][0]) if rows else 0
    logger.info(
        "published contract_anomaly: target=%s feature_set=%s flagged=%d retracted=%d",
        target[:24],
        feature_set,
        n,
        retracted,
    )
    return n
