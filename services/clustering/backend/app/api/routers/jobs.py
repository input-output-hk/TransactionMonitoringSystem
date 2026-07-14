"""Background-job polling (onboard + classify jobs share the table/enum)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import RepoDep
from app.api.schemas import LIST_LIMIT_DEFAULT, LIST_LIMIT_MAX, JobOut, ListPage
from app.storage.protocol import Repo

router = APIRouter(tags=["jobs"])


@router.get("/jobs", response_model=ListPage[JobOut])
def list_jobs(
    limit: int = Query(default=LIST_LIMIT_DEFAULT, ge=1, le=LIST_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
    repo: Repo = RepoDep,
) -> dict[str, Any]:
    rows = repo.list_jobs(limit=limit, offset=offset)
    return {"count": len(rows), "total": repo.count_jobs(), "data": rows}


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, repo: Repo = RepoDep) -> dict[str, Any]:
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job
