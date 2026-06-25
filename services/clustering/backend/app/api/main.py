"""FastAPI app assembly: lifespan (job worker), CORS, auth, and the routers.

Endpoints live in ``app/api/routers/`` (one module per resource group) and are
mounted twice:

- ``/api/v1`` — canonical, versioned; the only prefix in the OpenAPI schema and
  the contract an external UI/platform integrates against.
- ``/api`` — legacy alias (hidden from the schema) kept for the bundled UI and
  existing probes; it dies with the bundled UI.

Auth is per-router: every router except ``system`` (health/ready) carries the
``verify_api_key`` dependency, so probe exemption is by construction rather than
path matching (which would silently break under a second mount prefix).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import get_request_repo, verify_api_key
from app.api.routers import anomaly, contracts, jobs, labels, runs, system
from app.config import get_settings, setup_logging
from app.jobs import JobManager
from app.storage.clickhouse import ClickHouseRepo, select_repo_factory

logger = logging.getLogger(__name__)

# Re-exported for tests/back-compat: dependency_overrides keys on this function.
__all__ = ["app", "get_request_repo", "verify_api_key"]


def _guard_schema() -> None:
    """Fail fast if the live DB schema is behind the code (init SQL only runs on
    fresh volumes; existing volumes need ``python -m app.cli migrate``). A
    connectivity error is NOT fatal here — /api/ready reports that case — but a
    reachable DB with missing tables/columns would otherwise surface as confusing
    mid-request errors, so that aborts startup with the exact fix."""
    repo = ClickHouseRepo()
    try:
        missing = repo.missing_schema_objects()
    except Exception as exc:  # pragma: no cover - connectivity, not drift
        logger.warning("schema check skipped (ClickHouse unreachable at startup): %s", exc)
        return
    finally:
        repo.close()
    if missing:
        raise RuntimeError(
            "ClickHouse schema is behind the code; run "
            "`docker exec <backend> python -m app.cli migrate`. Missing: "
            + ", ".join(missing)
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background job worker (and re-enqueue interrupted jobs) for the
    lifetime of the app; stop it cleanly on shutdown."""
    setup_logging()
    _guard_schema()
    settings = get_settings()
    # Production safety: when REQUIRE_AUTH is set, an empty API_KEY (auth becomes
    # a no-op) or empty MODEL_SIGNING_KEYS (unsigned pickle blobs = code execution
    # on load) is a misconfiguration, not a warning. Fail fast, like the host
    # app's startup guards. Default-off keeps local/test/demo zero-config.
    if settings.require_auth:
        missing = [
            name
            for name, value in (
                ("API_KEY", settings.api_key),
                ("MODEL_SIGNING_KEYS", settings.model_signing_keys),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "REQUIRE_AUTH=1 but " + ", ".join(missing) + " not set. "
                "Refusing to start a network-exposed sidecar without "
                "authentication and signed model blobs. Set them, or unset "
                "REQUIRE_AUTH for a local/demo run."
            )
    if not settings.model_signing_keys:
        logger.warning(
            "MODEL_SIGNING_KEYS is not set: stored model blobs are unsigned. "
            "Fine for a local demo; REQUIRED in production (a tampered blob is "
            "pickle, i.e. code execution on load)."
        )
    # API_KEY set signals an intent to lock the deployment down, but a default
    # ClickHouse password means the data store is still wide open. Warn rather
    # than stay silent (kept quiet for zero-config local runs, where neither is set).
    if settings.api_key and settings.clickhouse_password == "tms":
        logger.warning(
            "CLICKHOUSE_PASSWORD is still the default ('tms') while API_KEY is set. "
            "Change the ClickHouse credentials before exposing this beyond localhost."
        )
    # host_ch (the default) reads each watched contract's data from the TMS's
    # ClickHouse, so its worker uses HostBackedRepo. A future downloading adapter
    # (its own ingestion) would use the default ClickHouseRepo. The request repo
    # (api/deps.py) resolves through the same helper so reads and the worker agree.
    host_backed = settings.host_backed
    manager = JobManager(repo_factory=select_repo_factory(settings))
    manager.start()
    app.state.job_manager = manager

    feed_task: asyncio.Task[None] | None = None
    feed_stop: asyncio.Event | None = None
    if host_backed and settings.feed_enabled:
        from app.service.scheduler import run_feed

        repo_factory = manager._repo_factory  # same factory the worker uses
        feed_stop = asyncio.Event()
        feed_task = asyncio.create_task(
            run_feed(manager=manager, repo_factory=repo_factory,
                     settings=settings, stop_event=feed_stop)
        )
        app.state.feed_stop = feed_stop
    try:
        yield
    finally:
        if feed_stop is not None:
            feed_stop.set()
        if feed_task is not None:
            try:
                await asyncio.wait_for(feed_task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                feed_task.cancel()
        manager.stop()


app = FastAPI(title="TMS Contract Anomaly Detection", version="0.1.0", lifespan=lifespan)
# Reached in-network from the TMS API's /api/clustering reverse-proxy, so no
# cross-origin access is needed by default; set CORS_ORIGINS to allow specific
# callers. No wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# Exact probe paths (both mounts) — matched exactly, not by suffix, so a future
# route that merely ends in /ready (e.g. a target named "ready") is still logged.
_PROBE_PATHS = frozenset(
    {"/api/health", "/api/ready", "/api/v1/health", "/api/v1/ready"}
)

_access_logger = logging.getLogger("app.api.access")


@app.middleware("http")
async def _log_requests(request: Request, call_next: Any) -> Any:
    """Minimum viable request observability: method, path, status, duration.
    Probe endpoints are skipped (they'd dominate the log at healthcheck cadence).
    Under LOG_FORMAT=json the fields land as structured keys via extra_fields."""
    path = request.url.path
    if path in _PROBE_PATHS:
        return await call_next(request)
    start = time.perf_counter()
    status = 500  # if call_next raises, log the request as a 500 before re-raising
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        _access_logger.info(
            "%s %s %s %sms",
            request.method,
            path,
            status,
            duration_ms,
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": path,
                    "status": status,
                    "duration_ms": duration_ms,
                }
            },
        )

_authed = [Depends(verify_api_key)]

api_router = APIRouter()
api_router.include_router(system.router)  # unauthenticated probes
api_router.include_router(contracts.router, dependencies=_authed)
api_router.include_router(jobs.router, dependencies=_authed)
api_router.include_router(runs.router, dependencies=_authed)
api_router.include_router(anomaly.router, dependencies=_authed)
api_router.include_router(labels.router, dependencies=_authed)

app.include_router(api_router, prefix="/api/v1")
app.include_router(api_router, prefix="/api", include_in_schema=False)
