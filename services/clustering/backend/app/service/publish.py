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
table for ``(network, target)`` to exactly the currently-flagged set. A tx that
was flagged before but is now benign/normal (re-fit reclassified it, a human
labeled its cluster benign, or a label was applied/cleared) is RETRACTED by
appending a superseding ``normal`` tombstone, so the host stops surfacing a
stale Contract Anomaly. ``tx_contract_anomaly`` is ``ReplacingMergeTree(scored_at)``
keyed by (network, tx_hash, target); the host reads FINAL, so the newest row
(real verdict or tombstone) wins. Tombstones are stamped ``now()`` so they
supersede the past row they retract yet are themselves superseded by any later
genuine re-flag (whose run/classify time is later still). Re-publishing the same
verdicts is therefore idempotent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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

# Verdicts that constitute the contract_anomaly attack surface (see module doc).
_PUBLISHED = (VERDICT_MALICIOUS, VERDICT_ANOMALY)

_COLUMNS = [
    "network", "tx_hash", "target", "cluster_id", "iso_score", "lof_score",
    "consensus", "votes", "verdict", "model_id", "feature_set", "evidence",
    "scored_at",
]


def _as_datetime(value: Any) -> datetime:
    """Coerce a ClickHouse-returned timestamp (datetime or 'YYYY-MM-DD HH:MM:SS'
    string) to a datetime for use as the ReplacingMergeTree version stamp."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _publish_batch(
    repo: Repo, target: str, network: str, feature_set: str,
) -> set[str]:
    """Resolve and publish the latest batch fit's flagged verdicts. Returns the
    set of tx_hashes published (empty if the target has no cluster run yet).

    Resolution is label-aware (``_resolve_verdicts`` folds in the target's manual
    labels), so a tx whose cluster was labeled benign resolves to ``benign`` and
    is excluded here, which lets the reconciliation step retract any stale row."""
    cluster_of, run = _run_membership(repo, target, feature_set)
    if not run or not cluster_of:
        return set()
    near = run["created_at"]
    votes = _anomaly_votes(repo, target, feature_set, near=near)
    tx_verdict, _ = _resolve_verdicts(repo, target, cluster_of, votes)
    flagged = {tx: v for tx, v in tx_verdict.items() if v in _PUBLISHED}
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
        rows.append([
            network, tx, target, int(cluster_of.get(tx, -1)),
            iso, lof, consensus, int(votes.get(tx, 0)), verdict,
            model_id, feature_set, "{}", stamp,
        ])
    db = repo._db  # type: ignore[attr-defined]
    repo.client.insert(  # type: ignore[attr-defined]
        f"{db}.tx_contract_anomaly", rows, column_names=_COLUMNS,
    )
    return set(flagged)


def _publish_online(
    repo: Repo, target: str, network: str, feature_set: str,
) -> set[str]:
    """Copy the target's flagged ``tx_classifications`` (incrementally-scored new
    txs) into ``tx_contract_anomaly``, in-database (both in ``tms_clustering``).
    Returns the set of tx_hashes published.

    A human benign/cleared label overrides the model verdict: txs the analyst
    labeled benign (or members of a cluster labeled benign — labelling writes an
    explicit per-member row) are excluded, so the reconciliation step can retract
    a stale alert. ``tx_labels`` reads use FINAL + ``deleted = 0``, so a CLEARED
    benign label correctly stops suppressing the tx again."""
    db = repo._db  # type: ignore[attr-defined]
    verdicts = ", ".join(f"'{v}'" for v in _PUBLISHED)
    params = {"net": network, "tgt": target, "fs": feature_set}
    # Built by concatenation (not an f-string) so the {name:Type} server-binding
    # placeholders stay literal while db / verdicts interpolate.
    where = (
        "target = {tgt:String} AND feature_set = {fs:String} "
        "AND verdict IN (" + verdicts + ") "
        "AND toString(tx_hash) NOT IN ("
        "  SELECT toString(tx_hash) FROM " + db + ".tx_labels FINAL "
        "  WHERE target = {tgt:String} AND deleted = 0 AND label = 'benign'"
        ")"
    )
    published = {
        str(r[0])
        for r in repo.client.query(  # type: ignore[attr-defined]
            "SELECT toString(tx_hash) FROM " + db + ".tx_classifications FINAL "
            "WHERE " + where,
            parameters=params,
        ).result_rows
    }
    if not published:
        return set()
    repo.client.command(  # type: ignore[attr-defined]
        "INSERT INTO " + db + ".tx_contract_anomaly (" + ", ".join(_COLUMNS) + ") "
        "SELECT {net:String} AS network, toString(tx_hash), target, cluster_id, "
        "iso_score, lof_score, consensus, votes, toString(verdict), model_id, "
        "toString(feature_set), '{}' AS evidence, toDateTime(scored_at) "
        "FROM " + db + ".tx_classifications FINAL WHERE " + where,
        parameters=params,
    )
    return published


def _retract_stale(
    repo: Repo, target: str, network: str, feature_set: str, *, keep: set[str],
) -> int:
    """Append a ``normal`` tombstone for every currently-published (non-normal) tx
    of this ``(network, target)`` that is NOT in ``keep`` (the freshly-published
    flagged set), so the host stops surfacing a now-benign/normal transaction.

    Returns the number of rows retracted. The tombstone is stamped ``now()`` so it
    supersedes the stale row (from a past run/classify) on FINAL, while any later
    genuine re-flag (a future run) supersedes the tombstone in turn."""
    db = repo._db  # type: ignore[attr-defined]
    current = {
        str(r[0])
        for r in repo.client.query(  # type: ignore[attr-defined]
            "SELECT DISTINCT toString(tx_hash) FROM " + db + ".tx_contract_anomaly "
            "FINAL WHERE network = {net:String} AND target = {tgt:String} "
            "AND verdict != {normal:String}",
            parameters={"net": network, "tgt": target, "normal": VERDICT_NORMAL},
        ).result_rows
    }
    stale = current - keep
    if not stale:
        return 0
    # Naive UTC, matching how _as_datetime stamps the real rows (ClickHouse
    # DateTime is tz-less UTC): an aware vs naive mix could order inconsistently
    # in the ReplacingMergeTree version compare. now() > any past run/classify
    # stamp (so the tombstone wins), yet < a future re-flag's run time.
    stamp = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        [network, tx, target, -1, float("nan"), float("nan"), 0.0, 0,
         VERDICT_NORMAL, "", feature_set, "{}", stamp]
        for tx in stale
    ]
    repo.client.insert(  # type: ignore[attr-defined]
        f"{db}.tx_contract_anomaly", rows, column_names=_COLUMNS,
    )
    return len(rows)


def publish_contract_anomaly(
    repo: Repo, target: str, *, network: str, feature_set: str = "shape",
) -> int:
    """Reconcile the host-visible contract_anomaly projection for a target.

    Publishes all currently-flagged verdicts (batch fit + online classify) and
    retracts any previously-published tx that is no longer flagged. Idempotent.
    Returns the number of flagged (non-tombstone) contract_anomaly rows now
    present for (network, target, feature_set) so callers can log coverage."""
    online_flagged = _publish_online(repo, target, network, feature_set)
    batch_flagged = _publish_batch(repo, target, network, feature_set)
    retracted = _retract_stale(
        repo, target, network, feature_set, keep=online_flagged | batch_flagged,
    )
    db = repo._db  # type: ignore[attr-defined]
    rows = repo.client.query(  # type: ignore[attr-defined]
        f"SELECT count() FROM {db}.tx_contract_anomaly FINAL "
        "WHERE network = {net:String} AND target = {tgt:String} "
        "AND feature_set = {fs:String} AND verdict != {normal:String}",
        parameters={"net": network, "tgt": target, "fs": feature_set,
                    "normal": VERDICT_NORMAL},
    ).result_rows
    n = int(rows[0][0]) if rows else 0
    logger.info(
        "published contract_anomaly: target=%s feature_set=%s flagged=%d retracted=%d",
        target[:24], feature_set, n, retracted,
    )
    return n
