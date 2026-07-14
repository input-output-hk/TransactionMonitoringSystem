"""Cluster runs: list/read, per-cluster transactions, the co-spend graph,
parameter evaluation, and ad-hoc custom clustering."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import RepoDep, analysis_slot, run_or_404
from app.contracts import normalize_target
from app.api.schemas import (
    ClusterRequest,
    ClusterRunAck,
    ClusterSummaryOut,
    ClusterTxPage,
    EvaluationOut,
    FeatureSet,
    GraphOut,
    ProjectionOut,
    RunOut,
)
from app.service import (
    build_graph,
    build_projection,
    cluster_summary_with_verdicts,
    cluster_target,
    cluster_transactions_with_verdicts,
    evaluate_target,
)
from app.storage.protocol import Repo

router = APIRouter(tags=["runs"])


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    target: str | None = Query(default=None), repo: Repo = RepoDep
) -> list[dict[str, Any]]:
    return repo.list_runs(target)


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str, repo: Repo = RepoDep) -> dict[str, Any]:
    return run_or_404(repo, run_id)


@router.get("/runs/{run_id}/clusters", response_model=list[ClusterSummaryOut])
def cluster_summary(run_id: str, repo: Repo = RepoDep) -> list[dict[str, Any]]:
    run = run_or_404(repo, run_id)
    return cluster_summary_with_verdicts(
        repo, run_id, run["target"], run["feature_set"], run_created_at=run["created_at"]
    )


@router.get("/runs/{run_id}/clusters/{cluster_id}/transactions", response_model=ClusterTxPage)
def cluster_transactions(
    run_id: str,
    cluster_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    run = run_or_404(repo, run_id)
    rows = cluster_transactions_with_verdicts(
        repo,
        run_id,
        run["target"],
        run["feature_set"],
        cluster_id,
        limit=limit,
        offset=offset,
        run_created_at=run["created_at"],
    )
    return {"run_id": run_id, "cluster_id": cluster_id, "transactions": rows}


@router.get("/runs/{run_id}/graph", response_model=GraphOut)
def run_graph(
    run_id: str,
    limit: int = Query(default=400, ge=1, le=2000),
    cluster: int | None = Query(default=None),
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    run_or_404(repo, run_id)
    return build_graph(repo, run_id, limit=limit, cluster=cluster)


@router.get(
    "/runs/{run_id}/projection", response_model=ProjectionOut, response_model_exclude_none=True
)
def run_projection(
    run_id: str,
    dims: int = Query(default=2, ge=2, le=3),
    limit: int = Query(default=1500, ge=1, le=5000),
    cluster: int | None = Query(default=None),
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    run_or_404(repo, run_id)
    return build_projection(repo, run_id, dims=dims, limit=limit, cluster=cluster)


@router.get("/evaluation", response_model=EvaluationOut)
def evaluation(
    target: str = Query(min_length=1),
    feature_set: FeatureSet = Query(default="shape"),
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    # Canonicalise (lowercases a hex policy id) so an ad-hoc run keys off the
    # same target the contract was onboarded under. Recall-safe: rejects nothing.
    with analysis_slot():
        return evaluate_target(repo, normalize_target(target), feature_set)


@router.post("/cluster", response_model=ClusterRunAck)
def run_cluster(req: ClusterRequest, repo: Repo = RepoDep) -> dict[str, Any]:
    with analysis_slot():
        return cluster_target(
            repo,
            normalize_target(req.target),
            req.feature_set,
            req.eps,
            req.min_samples,
            notes=req.notes,
        )
