"""Shared FastAPI dependencies and endpoint helpers.

Lives apart from ``main`` so routers can import these without a circular import,
and apart from the routers so tests can override ``get_request_repo`` once.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from typing import Any

from fastapi import Depends, Header, HTTPException

from app.config import get_settings
from app.service._common import target_in_jobs
from app.storage.clickhouse import ClickHouseRepo
from app.storage.protocol import Repo


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Require ``X-API-Key`` when ``API_KEY`` is configured; a no-op otherwise so
    local/demo runs stay zero-config. Attached per-router (every router except
    ``system``), so probe endpoints are exempt by construction — no path matching."""
    expected = get_settings().api_key
    # Compare BYTES: hmac.compare_digest raises TypeError on non-ASCII str, and
    # header values are latin-1 decodable — a crafted header must 401, not 500.
    supplied = (x_api_key or "").encode("utf-8", "surrogateescape")
    if expected and not hmac.compare_digest(supplied, expected.encode()):
        raise HTTPException(status_code=401, detail="missing or invalid API key")


def get_request_repo() -> Iterator[ClickHouseRepo]:
    """Per-request repository; its client is closed when the request finishes.

    A fresh client per request avoids sharing the (non-thread-safe) ClickHouse
    client across the threadpool that runs these sync endpoints.
    """
    repo = ClickHouseRepo()
    try:
        yield repo
    finally:
        repo.close()


RepoDep = Depends(get_request_repo)


def run_or_404(repo: Repo, run_id: str) -> dict[str, Any]:
    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return run


def reject_if_target_busy(repo: Repo, target: str) -> None:
    """Raise 409 if a job for ``target`` is already running, or 429 if the global
    in-flight cap is reached. Call inside ``job_manager.enqueue_lock`` so the
    check and the subsequent create_job are atomic across request threads."""
    inflight = repo.nonterminal_jobs()
    if target_in_jobs(inflight, target):
        raise HTTPException(status_code=409, detail=f"a job for {target} is already running")
    if len(inflight) >= get_settings().max_inflight_jobs:
        raise HTTPException(status_code=429, detail="too many jobs in progress; try again later")
