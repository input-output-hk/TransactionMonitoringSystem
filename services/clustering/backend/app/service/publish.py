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
  with a resolved verdict already; we copy the flagged ones straight across.

Only ``malicious`` / ``anomaly`` verdicts are published (``normal`` carries no
signal; ``benign`` is a human "cleared" label that must not raise a host band).
``tx_contract_anomaly`` is ``ReplacingMergeTree(scored_at)`` keyed by
(network, tx_hash, target); the version stamp is the run / classification time,
so re-publishing the same verdicts is a no-op (the host always reads FINAL).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.service.verdicts import (
    VERDICT_ANOMALY,
    VERDICT_MALICIOUS,
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
) -> int:
    """Resolve and publish the latest batch fit's flagged verdicts. Returns the
    number of rows written (0 if the target has no cluster run yet)."""
    cluster_of, run = _run_membership(repo, target, feature_set)
    if not run or not cluster_of:
        return 0
    near = run["created_at"]
    votes = _anomaly_votes(repo, target, feature_set, near=near)
    tx_verdict, _ = _resolve_verdicts(repo, target, cluster_of, votes)
    flagged = {tx: v for tx, v in tx_verdict.items() if v in _PUBLISHED}
    if not flagged:
        return 0

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
    return len(rows)


def _publish_online(
    repo: Repo, target: str, network: str, feature_set: str,
) -> None:
    """Copy the target's flagged ``tx_classifications`` (incrementally-scored new
    txs) into ``tx_contract_anomaly``, in-database (both in ``tms_clustering``)."""
    db = repo._db  # type: ignore[attr-defined]
    verdicts = ", ".join(f"'{v}'" for v in _PUBLISHED)
    repo.client.command(  # type: ignore[attr-defined]
        f"""
        INSERT INTO {db}.tx_contract_anomaly
            ({", ".join(_COLUMNS)})
        SELECT
            {{net:String}} AS network,
            toString(tx_hash), target, cluster_id, iso_score, lof_score,
            consensus, votes, toString(verdict), model_id,
            toString(feature_set), '{{}}' AS evidence, toDateTime(scored_at)
        FROM {db}.tx_classifications FINAL
        WHERE target = {{tgt:String}} AND feature_set = {{fs:String}}
          AND verdict IN ({verdicts})
        """,
        parameters={"net": network, "tgt": target, "fs": feature_set},
    )


def publish_contract_anomaly(
    repo: Repo, target: str, *, network: str, feature_set: str = "shape",
) -> int:
    """Publish all flagged verdicts (batch fit + online classify) for a target.

    Idempotent. Returns the total number of flagged contract_anomaly rows now
    present for (network, target, feature_set) so callers can log coverage."""
    _publish_online(repo, target, network, feature_set)
    _publish_batch(repo, target, network, feature_set)
    db = repo._db  # type: ignore[attr-defined]
    rows = repo.client.query(  # type: ignore[attr-defined]
        f"SELECT count() FROM {db}.tx_contract_anomaly FINAL "
        "WHERE network = {net:String} AND target = {tgt:String} "
        "AND feature_set = {fs:String}",
        parameters={"net": network, "tgt": target, "fs": feature_set},
    ).result_rows
    n = int(rows[0][0]) if rows else 0
    logger.info(
        "published contract_anomaly: target=%s feature_set=%s flagged=%d",
        target[:24], feature_set, n,
    )
    return n
