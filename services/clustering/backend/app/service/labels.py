"""Manual verdict labels — the write half of the human-judgement axis.

Cluster labels write one explicit per-tx label per current member
(``source='cluster'``) and therefore PROPAGATE: unlabeled siblings and future
txs classified into the cluster inherit the verdict. Single-tx labels
(``source='manual_tx'``) colour only their own transaction. The read half —
how these labels resolve into effective verdicts — lives in ``verdicts``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.service.verdicts import CLUSTER_VERDICTS
from app.storage.protocol import Repo

logger = logging.getLogger(__name__)


def _sync_host_projection(repo: Repo, target: str) -> None:
    """Reconcile the host-visible contract_anomaly projection after a label
    change, so a relabel/clear retracts or raises a host alert immediately
    instead of waiting for the next re-fit. Host-backed deployments only (the
    projection table lives in the engine db the host reads); best-effort: a
    failure here must not fail the label write (the next publish reconciles)."""
    if not get_settings().host_backed:
        return
    try:
        from app.service.publish import publish_contract_anomaly

        publish_contract_anomaly(
            repo, target, network=get_settings().cardano_network,
        )
    except Exception:  # noqa: BLE001 - projection sync is best-effort
        logger.warning(
            "host contract_anomaly projection sync failed for %s; "
            "the next publish will reconcile it", target[:24], exc_info=True,
        )


def label_cluster_members(
    repo: Repo,
    run_id: str,
    target: str,
    cluster_id: int,
    verdict: str,
    *,
    note: str = "",
) -> dict[str, Any]:
    """Bulk-apply ``verdict`` to a cluster's current members (one per-tx label each).
    Rejects the noise bucket (``cluster_id < 0``) with ValueError and an unknown
    cluster (no members in the run) with KeyError."""
    if cluster_id < 0:
        raise ValueError("cannot label the noise bucket")
    if verdict not in CLUSTER_VERDICTS:
        raise ValueError(f"verdict must be one of {CLUSTER_VERDICTS}")
    hashes = repo.cluster_member_hashes(run_id, cluster_id)
    if not hashes:
        raise KeyError(f"cluster {cluster_id} not found in run {run_id}")
    n = repo.set_tx_labels(target, hashes, verdict, source="cluster", note=note)
    _sync_host_projection(repo, target)
    return {"run_id": run_id, "cluster_id": cluster_id, "verdict": verdict, "labeled": n}


def clear_cluster_members(
    repo: Repo, run_id: str, target: str, cluster_id: int
) -> dict[str, Any]:
    """Remove the explicit labels from a cluster's current members (tombstone).
    Rejects the noise bucket (``cluster_id < 0``) for symmetry with labelling."""
    if cluster_id < 0:
        raise ValueError("cannot clear the noise bucket")
    hashes = repo.cluster_member_hashes(run_id, cluster_id)
    n = repo.clear_tx_labels(target, hashes)
    _sync_host_projection(repo, target)
    return {"run_id": run_id, "cluster_id": cluster_id, "cleared": n}


def label_transaction(
    repo: Repo, target: str, tx_hash: str, verdict: str, *, note: str = ""
) -> dict[str, Any]:
    """Apply a manual verdict (malicious/benign) to a single transaction (``source =
    manual_tx``). Unlike a cluster label this targets one ``tx_hash`` and does NOT
    propagate to its cluster siblings or to future transactions — it's a one-off
    judgement (e.g. on a noise-bucket outlier that belongs to no cluster), and the
    highest-precedence signal in ``compute_verdicts``. The ``manual_tx`` source is what
    keeps it out of the propagating set. Rejects any verdict but malicious/benign."""
    if verdict not in CLUSTER_VERDICTS:
        raise ValueError(f"verdict must be one of {CLUSTER_VERDICTS}")
    n = repo.set_tx_labels(target, [tx_hash], verdict, source="manual_tx", note=note)
    _sync_host_projection(repo, target)
    return {"target": target, "tx_hash": tx_hash, "verdict": verdict, "labeled": n}


def clear_transaction_label(
    repo: Repo, target: str, tx_hash: str
) -> dict[str, Any]:
    """Remove a single transaction's manual label (tombstone). No-op if it had none."""
    n = repo.clear_tx_labels(target, [tx_hash])
    _sync_host_projection(repo, target)
    return {"target": target, "tx_hash": tx_hash, "cleared": n}
