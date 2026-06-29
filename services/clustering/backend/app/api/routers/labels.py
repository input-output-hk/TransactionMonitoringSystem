"""Manual verdict labels — the human-judgement axis.

Cluster labels write one per-tx label per member (source='cluster') and
PROPAGATE to unlabeled siblings and future txs classified into the cluster.
Single-tx labels (source='manual_tx') colour ONLY their own transaction."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import RepoDep, run_or_404
from app.api.schemas import (
    ClusterClearAck,
    ClusterLabelAck,
    ClusterLabelRequest,
    TxClearAck,
    TxLabelAck,
    TxLabelRequest,
)
from app.contracts import normalize_target, normalize_tx_hash
from app.service import (
    clear_cluster_members,
    clear_transaction_label,
    label_cluster_members,
    label_transaction,
)
from app.storage.protocol import Repo

router = APIRouter(tags=["labels"])


@router.post("/runs/{run_id}/clusters/{cluster_id}/label", response_model=ClusterLabelAck)
def label_cluster(
    run_id: str,
    cluster_id: int,
    req: ClusterLabelRequest,
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    """Apply a manual verdict (malicious/benign) to a cluster's current members.

    Labels persist per tx_hash, so they survive reprocessing and propagate to future
    transactions that cluster alongside a labeled one (see ``compute_verdicts``).
    """
    run = run_or_404(repo, run_id)
    try:
        return label_cluster_members(
            repo, run_id, run["target"], cluster_id, req.verdict, note=req.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc


@router.post("/runs/{run_id}/clusters/{cluster_id}/clear-label", response_model=ClusterClearAck)
def clear_cluster_label(run_id: str, cluster_id: int, repo: Repo = RepoDep) -> dict[str, Any]:
    """Remove the manual verdict from a cluster's current members."""
    run = run_or_404(repo, run_id)
    try:
        return clear_cluster_members(repo, run_id, run["target"], cluster_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/contracts/{target}/transactions/{tx_hash}/label", response_model=TxLabelAck)
def label_tx(
    target: str, tx_hash: str, req: TxLabelRequest, repo: Repo = RepoDep
) -> dict[str, Any]:
    """Apply a manual verdict (malicious/benign) to a single transaction.

    Targets one ``tx_hash`` — unlike a cluster label it does NOT propagate to future
    transactions (it generalises to no pattern). Use it to record a judgement on a
    specific tx, e.g. a noise-bucket outlier that belongs to no cluster.
    """
    target = normalize_target(target)
    tx_hash = normalize_tx_hash(tx_hash)
    try:
        return label_transaction(repo, target, tx_hash, req.verdict, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/contracts/{target}/transactions/{tx_hash}/clear-label", response_model=TxClearAck)
def clear_tx_label(target: str, tx_hash: str, repo: Repo = RepoDep) -> dict[str, Any]:
    """Remove a single transaction's manual label."""
    return clear_transaction_label(repo, normalize_target(target), normalize_tx_hash(tx_hash))
