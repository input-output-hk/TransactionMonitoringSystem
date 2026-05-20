"""FastAPI application with WebSocket support for real-time transaction display"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, Security
from fastapi.middleware.cors import CORSMiddleware

from app.auth import verify_api_key

from app.config import settings

# Configure logging before importing modules that emit log records at import time
# (e.g. app.analysis.scorer_config which logs the config file it loaded).
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from app.rate_limit import RateLimitMiddleware, RateLimiter
from app.db import postgres, clickhouse, raw_store
from app.api import transactions, entities, lifecycle, analysis, archive
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
    """
    while True:
        try:
            await coro_fn()
            return  # clean return means disconnect() was called
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[supervisor] {label} crashed: {e!r} — restarting in 5 s")
            await asyncio.sleep(5)


async def broadcast_lifecycle_event(event: dict):
    """Broadcast lifecycle events to all connected WebSocket clients"""
    if not active_connections:
        return
    disconnected = []
    for connection in active_connections:
        try:
            await connection.send_json({
                "type": "lifecycle",
                "data": event,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            disconnected.append(connection)
    for conn in disconnected:
        if conn in active_connections:
            active_connections.remove(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown"""
    global ogmios_client

    # Emit auth dev-mode warning here so logging is already configured.
    # Dev mode (empty API_KEYS) is useful for local work but must be an
    # explicit choice — refuse to start without TMS_ALLOW_DEV_MODE=1 so an
    # accidental production deploy with a blank `.env` does not end up
    # running an open API. TMS_ALLOW_DEV_MODE is read through pydantic so
    # it can live in the layered `.env` files, not just the shell env.
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
    if not settings.CLICKHOUSE_PASSWORD:
        logger.warning("CLICKHOUSE_PASSWORD is empty — ClickHouse is unauthenticated (development mode)")

    # Start rate-limiter cleanup task
    if settings.RATE_LIMIT_ENABLED:
        _limiter.start_cleanup()
        logger.info(
            f"Rate limiting enabled: {settings.RATE_LIMIT_REQUESTS} req"
            f" / {settings.RATE_LIMIT_WINDOW_SECONDS}s per key"
        )

    # Startup
    try:
        logger.info("Initializing databases...")
        await postgres.init_pool()
        await postgres.execute_schema()
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
        asyncio.create_task(_supervised("mempool_monitor", ogmios_client.run_mempool_monitor))
        logger.info(f"Ogmios client started for {settings.CARDANO_NETWORK} at {settings.OGMIOS_WS_URL}")

    except Exception as e:
        logger.error(f"Failed to initialize: {e}")
        raise

    yield

    # Shutdown
    logger.info("Shutting down...")
    if settings.RATE_LIMIT_ENABLED:
        _limiter.stop_cleanup()
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


app = FastAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    lifespan=lifespan,
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

# CORS: registered last → outermost → executes first, wraps rate limiter
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(ui.router)
app.include_router(websocket.router)
app.include_router(transactions.router)
app.include_router(entities.router)
app.include_router(lifecycle.router)
app.include_router(analysis.router)
app.include_router(archive.router)


@app.get("/health")
async def health():
    """Liveness probe. Intentionally minimal and unauthenticated so load
    balancers / orchestration platforms can hit it without a key.

    Detailed operational state (pipeline_state, sync lag, circuit breaker,
    WebSocket connection count) lives at ``/health/detail`` and requires
    an API key so external scanners cannot enumerate internals.
    """
    return {"status": "healthy"}


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
