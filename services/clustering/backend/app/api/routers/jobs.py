"""Background-job polling (onboard + classify jobs share the table/enum)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import RepoDep
from app.api.schemas import JobOut
from app.storage.protocol import Repo

router = APIRouter(tags=["jobs"])


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(repo: Repo = RepoDep) -> list[dict[str, Any]]:
    return repo.list_jobs()


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, repo: Repo = RepoDep) -> dict[str, Any]:
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job
