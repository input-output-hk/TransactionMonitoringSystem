"""Shared FastAPI dependencies and endpoint helpers.

Lives apart from ``main`` so routers can import these without a circular import,
and apart from the routers so tests can override ``get_request_repo`` once.
"""

from __future__ import annotations

import hmac
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import Depends, Header, HTTPException

from app.config import get_settings
from app.service._common import target_in_jobs
from app.storage.clickhouse import ClickHouseRepo, select_repo_factory
from app.storage.protocol import Repo

# Bounds concurrent heavy analysis runs (see analysis_slot). Lazily created so
# it binds to the configured cap, and rebuildable in tests via _reset_analysis_slot.
_analysis_semaphore: threading.Semaphore | None = None
_analysis_semaphore_lock = threading.Lock()


def _get_analysis_semaphore() -> threading.Semaphore:
    global _analysis_semaphore
    if _analysis_semaphore is None:
        with _analysis_semaphore_lock:
            if _analysis_semaphore is None:
                _analysis_semaphore = threading.Semaphore(
                    get_settings().max_concurrent_analyses
                )
    return _analysis_semaphore


@contextmanager
def analysis_slot() -> Iterator[None]:
    """Bound CONCURRENT ad-hoc analysis runs (full-window load + DBSCAN + the
    O(n^2) silhouette). Excess concurrent callers WAIT for a slot rather than
    running simultaneously and overloading ClickHouse / the box; they are never
    rejected, so an analyst's run still completes (just later)."""
    sem = _get_analysis_semaphore()
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


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

    The repo CLASS must match the job worker's (see ``select_repo_factory``):
    on host_ch the feature reads behind the graph / tx-list endpoints live in
    the host TMS's tables, not the module's own (empty) raw-tx tables.
    """
    repo = select_repo_factory(get_settings())()
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
