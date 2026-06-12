"""FastAPI application with WebSocket support for real-time transaction display"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

from pathlib import Path

from fastapi import FastAPI, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth import verify_api_key

from app.config import settings

# Configure logging before importing modules that emit log records at import time
# (e.g. app.analysis.scorer_config which logs the config file it loaded).
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from app.rate_limit import (
    RateLimitMiddleware,
    RateLimiter,
    start_all_cleanups,
    stop_all_cleanups,
)
from app.db import postgres, clickhouse, raw_store
from app.api import transactions, entities, lifecycle, analysis, archive, auth as auth_api, users as users_api
from app.tasks import analysis as analysis_task
from app.routers import ui, websocket

# Global state
active_connections: List = []
ogmios_client = None


async def _supervised(label: str, coro_fn):
    """Restart coro_fn if it exits with an unexpected exception.

    Normal shutdown (self._running = False) causes the coroutine to return
    without raising, so we don't restart in that case.
    asyncio.CancelledError propagates to let the task be properly cancelled.

    Restart delay backs off exponentially (a persistent bug previously
    became an infinite fixed 5 s crash loop hammering logs and downstream
    services) and resets after a stable run, so a one-off crash recovers
    fast while a hard failure settles at the ceiling.
    """
    delay = settings.SUPERVISOR_BACKOFF_BASE_SECONDS
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        try:
            await coro_fn()
            return  # clean return means disconnect() was called
        except asyncio.CancelledError:
            return
        except Exception as e:
            if loop.time() - started >= settings.SUPERVISOR_STABLE_RESET_SECONDS:
                delay = settings.SUPERVISOR_BACKOFF_BASE_SECONDS
            logger.error(
                f"[supervisor] {label} crashed: {e!r} — restarting in {delay:.0f} s"
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, settings.SUPERVISOR_BACKOFF_MAX_SECONDS)


async def broadcast_lifecycle_event(event: dict):
    """Broadcast lifecycle events to all connected WebSocket clients.

    Non-blocking: enqueues onto per-client bounded queues (see
    routers/websocket.broadcast). The previous implementation awaited
    send_json per client from the ingestion path, so one slow client
    stalled block processing for the whole process.
    """
    if not active_connections:
        return
    await websocket.broadcast({
        "type": "lifecycle",
        "data": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _validate_startup_settings() -> None:
    """Fail-fast configuration guards, run before any service starts.

    Dev mode (empty API_KEYS) is useful for local work but must be an
    explicit choice — refuse to start without TMS_ALLOW_DEV_MODE=1 so an
    accidental production deploy with a blank `.env` does not end up
    running an open API. TMS_ALLOW_DEV_MODE is read through pydantic so
    it can live in the layered `.env` files, not just the shell env.
    """
    from app.auth import _dev_mode
    allow_dev_mode = (
        settings.TMS_ALLOW_DEV_MODE.strip() == "1"
        or os.environ.get("TMS_ALLOW_DEV_MODE", "").strip() == "1"
    )
    if _dev_mode:
        if not allow_dev_mode:
            raise RuntimeError(
                "API_KEYS is empty and TMS_ALLOW_DEV_MODE != '1'. "
                "Refusing to start in open-API mode. Set API_KEYS=<keys> for "
                "production, or set TMS_ALLOW_DEV_MODE=1 in .env (or the "
                "shell environment) for local dev."
            )
        logger.warning("No API keys configured — API is open (development mode)")
    # Same fail-fast posture as API_KEYS: an empty ClickHouse password is a
    # deliberate dev-mode choice, never an accident a production deploy
    # discovers later. (The port binds to loopback in compose, so this is
    # defence-in-depth against other local processes/containers.)
    if not settings.CLICKHOUSE_PASSWORD:
        if not allow_dev_mode:
            raise RuntimeError(
                "CLICKHOUSE_PASSWORD is empty and TMS_ALLOW_DEV_MODE != '1'. "
                "Refusing to start against an unauthenticated ClickHouse. "
                "Set CLICKHOUSE_PASSWORD (and the matching docker-compose "
                "env) for production, or TMS_ALLOW_DEV_MODE=1 for local dev."
            )
        logger.warning("CLICKHOUSE_PASSWORD is empty — ClickHouse is unauthenticated (development mode)")
    # Capped ClickHouse payloads make the raw store the ONLY full copy of
    # oversized (attack-shaped) transactions; running capped without the
    # store means those txs could never be scored at full fidelity.
    if settings.RAW_DATA_MAX_BYTES > 0 and not settings.RAW_STORE_ENABLED:
        raise RuntimeError(
            "RAW_DATA_MAX_BYTES > 0 caps ClickHouse raw_data but "
            "RAW_STORE_ENABLED is False: oversized transactions would have "
            "NO full payload copy. Enable RAW_STORE_ENABLED or set "
            "RAW_DATA_MAX_BYTES=0."
        )
    # CORS '*' (or unset) is a dev convenience, never a production posture:
    # same fail-fast as the credentials above. Keys configured = production.
    origins = settings.cors_allow_origins_list
    if not _dev_mode and (not origins or "*" in origins) and not allow_dev_mode:
        raise RuntimeError(
            "CORS_ALLOW_ORIGINS is '*' or empty with API keys configured. "
            "Set the explicit dashboard origin(s) for production, or "
            "TMS_ALLOW_DEV_MODE=1 for local dev."
        )
    # trusted_proxy_networks re-parses TRUSTED_PROXY_CIDRS on every request;
    # a malformed CIDR would otherwise surface as a per-request failure
    # (app.net degrades it to untrusted-peer, silently disabling proxy
    # trust). Parse once here so a typo refuses to start with a clear
    # message instead.
    if settings.TRUSTED_PROXY_ENABLED:
        try:
            settings.trusted_proxy_networks
        except ValueError as exc:
            raise RuntimeError(
                f"TRUSTED_PROXY_CIDRS is malformed: {exc}. Fix the CIDR "
                "list (comma-separated networks such as 172.18.0.1/32) or "
                "set TRUSTED_PROXY_ENABLED=false."
            ) from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown"""
    global ogmios_client

    # Emit dev-mode warnings here so logging is already configured.
    _validate_startup_settings()

    # Start the eviction loop for EVERY registered rate limiter (the global
    # IP/key limiter in this module, the per-email limiter in app.api.auth,
    # and the WS handshake limiter in app.routers.websocket). Done via the
    # registry so no limiter is forgotten — an unstarted cleanup task means
    # unbounded memory growth for attacker-controlled keys.
    start_all_cleanups()
    if settings.RATE_LIMIT_ENABLED:
        logger.info(
            f"Rate limiting enabled: {settings.RATE_LIMIT_REQUESTS} req"
            f" / {settings.RATE_LIMIT_WINDOW_SECONDS}s per key"
        )

    # Startup
    try:
        logger.info("Initializing databases...")
        await postgres.init_pool()
        await postgres.execute_schema()
        # Magic-link auth tables (users, magic_link_tokens, user_sessions).
        # Idempotent + handles legacy `users` table migration.
        from app.auth.schema import execute_auth_schema
        await execute_auth_schema()
        clickhouse.init_client()
        clickhouse.execute_schema()
        if settings.RAW_STORE_ENABLED:
            raw_store.init_store()
        logger.info("Databases initialized")

        # Start Analysis Engine background task
        if settings.ANALYSIS_ENGINE_ENABLED:
            analysis_task.start()
            logger.info(
                f"Analysis Engine started "
                f"(interval={settings.ANALYSIS_ENGINE_INTERVAL_SECONDS}s, "
                f"batch={settings.ANALYSIS_ENGINE_BATCH_SIZE})"
            )

        # Start Ogmios ingestion
        from app.ingestion.ogmios_client import OgmiosClient

        ogmios_client = OgmiosClient(on_lifecycle_event=broadcast_lifecycle_event)
        websocket.set_active_connections(active_connections)

        asyncio.create_task(_supervised("chain_sync", ogmios_client.run_chain_sync))
        asyncio.create_task(_supervised("mempool_monitor", ogmios_client.mempool.run))
        logger.info(f"Ogmios client started for {settings.CARDANO_NETWORK} at {settings.OGMIOS_WS_URL}")

    except Exception as e:
        logger.error(f"Failed to initialize: {e}")
        raise

    yield

    # Shutdown
    logger.info("Shutting down...")
    stop_all_cleanups()
    if settings.ANALYSIS_ENGINE_ENABLED:
        analysis_task.stop()
    if ogmios_client:
        await ogmios_client.disconnect()
    await postgres.close_pool()
    clickhouse.close_client()
    clickhouse.shutdown_executor()
    if settings.RAW_STORE_ENABLED:
        raw_store.shutdown_executor()
    logger.info("Shutdown complete")


# /docs, /redoc and /openapi.json enumerate the whole admin attack surface
# and sit in the rate-limit exemption list, so they are exposed only in dev
# mode or behind an explicit production opt-in.
from app.auth import _dev_mode as _auth_dev_mode  # noqa: E402

_docs_enabled = _auth_dev_mode or settings.TMS_API_DOCS_ENABLED

app = FastAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    description="""
    Cardano Transaction Monitoring System API.

    **Network Parameter**: All endpoints accept an optional `network` parameter.
    - Options: `mainnet`, `preprod`, or `preview`
    - Default: `preprod` (if not specified)
    """
)

# Middleware registration — Starlette applies middleware in LIFO order,
# so the last registered middleware is the outermost (executes first on request).
#
# Desired execution order (request → response):
#   CORS → RateLimiter → Routes
#
# This ensures CORS headers are present on ALL responses, including 429s.

# RateLimiter: registered first → innermost → executes second
if settings.RATE_LIMIT_ENABLED:
    _limiter = RateLimiter(
        max_requests=settings.RATE_LIMIT_REQUESTS,
        window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
    )
    app.add_middleware(RateLimitMiddleware, limiter=_limiter)

# CORS: registered last → outermost → executes first, wraps rate limiter.
# Origins are configurable (CORS_ALLOW_ORIGINS, comma-separated); the "*"
# default keeps the demo SPA / local vite dev server working. Tighten to
# the dashboard origin in production deployments.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers. When the built SPA is available at /app/frontend-dist
# (produced by the Dockerfile's frontend-build stage), it owns "/" and the
# legacy embedded UI router is skipped. Local dev without a build still
# falls back to the embedded UI.
FRONTEND_DIST = Path("/app/frontend-dist")
_spa_present = FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file()

if not _spa_present:
    app.include_router(ui.router)
app.include_router(websocket.router)
app.include_router(transactions.router)
app.include_router(entities.router)
app.include_router(lifecycle.router)
app.include_router(analysis.router)
app.include_router(archive.router)
app.include_router(auth_api.router)
app.include_router(users_api.router)


@app.get("/health")
async def health():
    """Liveness probe. Intentionally minimal and unauthenticated so load
    balancers / orchestration platforms can hit it without a key.

    Detailed operational state (pipeline_state, sync lag, circuit breaker,
    WebSocket connection count) lives at ``/health/detail`` and requires
    an API key so external scanners cannot enumerate internals.
    """
    return {"status": "healthy"}


@app.get("/health/ready")
async def health_ready():
    """Readiness probe: 503 while the ingestion pipeline is DOWN.

    /health stays a pure liveness signal (the process is up), which kept
    load balancers routing to an instance that was ingesting nothing
    (audit finding). Orchestrators should gate traffic on THIS endpoint.
    Unauthenticated like /health, but exposes only the coarse state word,
    no internals.
    """
    state = "UNKNOWN"
    if ogmios_client:
        state = ogmios_client.status["pipeline_state"]
    if state == "DOWN":
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "pipeline_state": state},
        )
    return {"status": "ready", "pipeline_state": state}


@app.get("/health/detail")
async def health_detail(api_key: str = Security(verify_api_key)):
    """Full operational state: network, pipeline, Ogmios, connections."""
    result = {
        "status": "healthy",
        "network": settings.CARDANO_NETWORK,
        "connections": len(active_connections),
        "pipeline_state": "UNKNOWN",
    }
    if ogmios_client:
        ogmios_status = ogmios_client.status
        result["ogmios"] = ogmios_status
        result["pipeline_state"] = ogmios_status["pipeline_state"]
    return result


# SPA mount goes LAST so /api/*, /health, /ws, etc. still match their routes
# first. Hashed asset filenames are served directly by StaticFiles; any other
# path (client-side router URL like /attacks/123) falls through to index.html.
if _spa_present:
    app.mount(
        "/assets",
        StaticFiles(directory=str(FRONTEND_DIST / "assets")),
        name="spa-assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
