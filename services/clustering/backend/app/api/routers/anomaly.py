"""Anomaly runs: ad-hoc detection, run listing, top candidates (with the
effective per-tx verdict), and custom-run deletion."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import RepoDep, analysis_slot
from app.api.schemas import (
    AnomalyDetectAck,
    AnomalyRequest,
    AnomalyRunDeleteAck,
    AnomalyRunOut,
    AnomalyTopPage,
)
from app.contracts import normalize_target
from app.service import detect_anomalies_for_target, top_anomalies_with_verdicts
from app.storage.protocol import Repo

router = APIRouter(tags=["anomaly"])


@router.post("/anomaly", response_model=AnomalyDetectAck)
def run_anomaly(req: AnomalyRequest, repo: Repo = RepoDep) -> dict[str, Any]:
    with analysis_slot():
        return detect_anomalies_for_target(
            repo,
            normalize_target(req.target),
            req.feature_set,
            eps=req.eps,
            min_samples=req.min_samples,
            top_quantile=req.top_quantile,
        )


@router.get("/anomaly-runs", response_model=list[AnomalyRunOut])
def list_anomaly_runs(
    target: str | None = Query(default=None), repo: Repo = RepoDep
) -> list[dict[str, Any]]:
    return repo.list_anomaly_runs(target)


@router.get("/anomaly-runs/{run_id}/top", response_model=AnomalyTopPage)
def anomaly_top(
    run_id: str,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    try:
        return top_anomalies_with_verdicts(repo, run_id, limit=limit, offset=offset)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"anomaly run {run_id} not found") from None


@router.delete("/anomaly-runs/{run_id}", response_model=AnomalyRunDeleteAck)
def delete_anomaly_run(run_id: str, repo: Repo = RepoDep) -> dict[str, Any]:
    """Delete a user-created anomaly run. System runs (produced by onboarding) are
    canonical for scoring/verdicts and may not be deleted."""
    run = repo.get_anomaly_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"anomaly run {run_id} not found")
    if run.get("origin") == "system":
        raise HTTPException(
            status_code=403,
            detail="system-generated anomaly runs cannot be deleted",
        )
    repo.delete_anomaly_run(run_id)
    return {"deleted": True, "run_id": run_id}
