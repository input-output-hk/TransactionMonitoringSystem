"""Contract onboarding/management + the offline registry preview + ingested
targets. Mutations that enqueue work hold the JobManager's ``enqueue_lock`` so
the busy-check and job creation are atomic across request threads."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.deps import RepoDep, reject_if_target_busy
from app.api.schemas import (
    LIST_LIMIT_DEFAULT,
    LIST_LIMIT_MAX,
    ClassifyJobAck,
    ContractDeleteAck,
    ContractOut,
    ContractRequest,
    IdentifyOut,
    JobAck,
    LatestInteractionsPage,
    ListPage,
    RenameRequest,
    TargetOut,
)
from app.config import get_settings
from app.contracts import classify_target, normalize_target
from app.ids import new_id
from app.registry import lookup_label, script_hash_for
from app.service import latest_interactions_with_verdicts
from app.service._common import target_in_jobs
from app.storage.protocol import Repo

router = APIRouter(tags=["contracts"])


@router.get("/targets", response_model=ListPage[TargetOut])
def list_targets(
    limit: int = Query(default=LIST_LIMIT_DEFAULT, ge=1, le=LIST_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    rows = repo.list_targets(limit=limit, offset=offset)
    return {"count": len(rows), "total": repo.count_targets(), "data": rows}


@router.get("/registry/identify", response_model=IdentifyOut)
def identify_target(target: str = Query(min_length=1, max_length=256)) -> dict[str, Any]:
    """Resolve a target to its script hash + registry label (offline, registry-only).

    Used by the Add Contract form for a live preview while the user types, so an
    unparseable target returns ``valid: false`` rather than an error.
    """
    value = target.strip()
    try:
        target_type = classify_target(value)
    except ValueError:
        return {"valid": False, "target_type": None, "script_hash": None, "label": ""}
    return {
        "valid": True,
        "target_type": target_type,
        "script_hash": script_hash_for(value, target_type),
        "label": lookup_label(value, target_type),
    }


@router.post("/contracts", response_model=JobAck)
def create_contract(req: ContractRequest, request: Request, repo: Repo = RepoDep) -> dict[str, Any]:
    """Register a contract and enqueue the canonical onboarding pipeline."""
    # Canonical casing on write AND on every {target} path read below — half-applied
    # normalization would make POST and GET disagree about the same policy id.
    target = normalize_target(req.target)
    try:
        target_type = classify_target(target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Fail fast on a target the active data source cannot serve, instead of
    # queueing a job that deterministically fails at fetch time. The host-backed
    # source indexes transactions by address only (not by policy id), so reject a
    # policy target up front. Other sources accept both, so the gate is scoped to
    # host_ch (mirrors host_ch.metadata()'s own rejection).
    if target_type == "policy" and get_settings().host_backed:
        raise HTTPException(
            status_code=422,
            detail=(
                "Policy-id targets are not supported in the host-backed deployment: "
                "the host TMS indexes transactions by address, not by policy id. "
                "Provide an addr… address."
            ),
        )

    manager = request.app.state.job_manager
    with manager.enqueue_lock:  # atomic guard-then-create across request threads
        reject_if_target_busy(repo, target)
        job_id = new_id("job")
        # Keep an existing custom name when re-adding without one — the pending row
        # we write here is what the pipeline reads back as its label preset, so an
        # empty label would otherwise clobber a previously-set name.
        prev = repo.get_contract(target)
        label = req.label.strip() or (prev or {}).get("label", "")
        repo.save_contract(
            {
                "target": target,
                "target_type": target_type,
                "status": "pending",
                "requested_max_txs": req.max_txs or 0,
                "label": label,
            }
        )
        repo.create_job(job_id, target, target_type, req.max_txs or 0, int(req.reprocess))
        manager.enqueue(job_id)
    return {"job_id": job_id, "target": target, "target_type": target_type}


@router.get("/contracts", response_model=ListPage[ContractOut])
def list_contracts(
    limit: int = Query(default=LIST_LIMIT_DEFAULT, ge=1, le=LIST_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    rows = repo.list_contracts(limit=limit, offset=offset)
    return {"count": len(rows), "total": repo.count_contracts(), "data": rows}


@router.get("/contracts/{target}", response_model=ContractOut)
def get_contract(target: str, repo: Repo = RepoDep) -> dict[str, Any]:
    normalized = normalize_target(target)
    contract = repo.get_contract(normalized)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"contract {target} not found")
    settings = get_settings()
    if settings.history_enabled:
        from app.service.history import history_status

        contract["history_tx_count"] = repo.history_tx_count(normalized)
        contract["history_status"] = history_status(settings, normalized)
    return contract


@router.patch("/contracts/{target}", response_model=ContractOut)
def rename_contract(target: str, req: RenameRequest, repo: Repo = RepoDep) -> dict[str, Any]:
    """Set a contract's display name (``label``) without re-running the pipeline."""
    contract = repo.update_contract_label(normalize_target(target), req.label.strip())
    if contract is None:
        raise HTTPException(status_code=404, detail=f"contract {target} not found")
    return contract


@router.delete("/contracts/{target}", response_model=ContractDeleteAck)
def delete_contract(target: str, request: Request, repo: Repo = RepoDep) -> dict[str, Any]:
    """Hard-delete a contract and all its data across every table.

    Refused with 409 while a job for the target is in flight: the single worker
    would be mid-write, and the purge spans the tables it touches. The busy check
    and the delete run under ``enqueue_lock`` so a job can't be enqueued between
    them."""
    target = normalize_target(target)
    if repo.get_contract(target) is None:
        raise HTTPException(status_code=404, detail=f"contract {target} not found")
    manager = request.app.state.job_manager
    with manager.enqueue_lock:
        if target_in_jobs(repo.nonterminal_jobs(), target):
            raise HTTPException(
                status_code=409,
                detail=f"a job for {target} is running; stop or wait for it before deleting",
            )
        repo.delete_contract(target)
    return {"deleted": True, "target": target}


@router.post("/contracts/{target}/classify-new", response_model=ClassifyJobAck)
def classify_new_contract(target: str, request: Request, repo: Repo = RepoDep) -> dict[str, Any]:
    """Download the contract's latest not-yet-classified transactions and score
    them against its frozen model (incremental; no full re-cluster). Enqueues a
    ``classify`` job and returns its id for polling."""
    target = normalize_target(target)
    contract = repo.get_contract(target)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"contract {target} not found")

    manager = request.app.state.job_manager
    with manager.enqueue_lock:  # atomic guard-then-create across request threads
        reject_if_target_busy(repo, target)
        job_id = new_id("job")
        repo.create_job(job_id, target, contract["target_type"], 0, 0, kind="classify")
        manager.enqueue(job_id)
    return {"job_id": job_id, "target": target, "kind": "classify"}


@router.get("/contracts/{target}/latest", response_model=LatestInteractionsPage)
def list_latest_interactions(
    target: str,
    feature_set: str = Query(default="shape"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    """The latest ``limit`` transactions for a target (newest first), each with a LIVE
    verdict — or ``unclassified`` (classified=False) when it's in no cluster run and
    hasn't been online-scored yet. The recency-first feed behind the Latest tab; the
    verdict is recomputed against each cluster's current label state, so a relabel is
    reflected at once."""
    target = normalize_target(target)
    return latest_interactions_with_verdicts(repo, target, feature_set, limit=limit, offset=offset)
