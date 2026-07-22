"""FastAPI application with WebSocket support for real-time transaction display"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, FastAPI, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth import verify_api_key
from app.config import DEFAULT_DEV_POSTGRES_PASSWORD, settings
from app.csrf import CSRFMiddleware

# Configure logging before importing modules that emit log records at import time
# (e.g. app.analysis.scorer_config which logs the config file it loaded). One
# shared setup for the app and the scripts (app.logging_utils.setup_logging),
# which also attaches the secret-redaction filter to the root handlers.
from app.logging_utils import configure_access_log_redaction, setup_logging
from app.utils.datetime_utils import format_iso_utc, to_aware_utc

setup_logging()
logger = logging.getLogger(__name__)

# Magic-link tokens ride in `/api/auth/verify?token=...`. setup_logging already
# scrubs the root handlers, but uvicorn attaches its own access-log handler
# later via dictConfig; this logger-level filter survives that and catches the
# credential in every access line (review finding).
configure_access_log_redaction()

from app import leader, notifications
from app.api import (
    analysis,
    archive,
    backfill,
    entities,
    lifecycle,
    notifications_config,
    transactions,
)
from app.api import (
    auth as auth_api,
)
from app.api import (
    clustering as clustering_api,
)
from app.api import (
    users as users_api,
)
from app.db import clickhouse, postgres, raw_store
from app.rate_limit import (
    RateLimiter,
    RateLimitMiddleware,
    start_all_cleanups,
    stop_all_cleanups,
)
from app.routers import websocket
from app.tasks import analysis as analysis_task
from app.tasks import housekeeping as housekeeping_task
from app.tasks import notifications as notifications_task

# Global state
active_connections: list = []
ogmios_client = None
# Strong references to the ingestion supervisor tasks. asyncio holds only weak
# references to tasks, so a bare create_task() can be garbage-collected mid-run
# (ingestion silently dies); keeping them here pins their lifetime and lets
# shutdown await them. See lifespan().
_ingestion_tasks: list[asyncio.Task] = []
# The standby retry loop (app.leader guard), while this instance has not yet
# become leader. None once promoted (or if it was never needed).
_leader_standby_task: asyncio.Task | None = None
# Whether THIS process has started ingestion + the analysis engine — tracked
# separately from leader.is_leader() so shutdown does the right thing whether
# the guard is enabled or not (see _start_leader_duties / lifespan).
_leader_duties_started = False


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
            logger.error(f"[supervisor] {label} crashed: {e!r} — restarting in {delay:.0f} s")
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
    await websocket.broadcast(
        {
            "type": "lifecycle",
            "data": event,
            "timestamp": format_iso_utc(datetime.now(UTC)),
        }
    )


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
        logger.warning(
            "CLICKHOUSE_PASSWORD is empty — ClickHouse is unauthenticated (development mode)"
        )
    # A baked-in default Postgres password is a guessable credential, never a
    # production posture. Same fail-fast as API_KEYS / CLICKHOUSE_PASSWORD:
    # refuse to start on the known dev default unless dev mode is explicit.
    if settings.POSTGRES_PASSWORD == DEFAULT_DEV_POSTGRES_PASSWORD:
        if not allow_dev_mode:
            raise RuntimeError(
                "POSTGRES_PASSWORD is the well-known dev default. Refusing to "
                "start on a guessable credential. Set POSTGRES_PASSWORD (and "
                "the matching docker-compose env) for production, or "
                "TMS_ALLOW_DEV_MODE=1 for local dev."
            )
        logger.warning(
            "POSTGRES_PASSWORD is the dev default — guessable credential (development mode)"
        )
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
            _ = settings.trusted_proxy_networks
        except ValueError as exc:
            raise RuntimeError(
                f"TRUSTED_PROXY_CIDRS is malformed: {exc}. Fix the CIDR "
                "list (comma-separated networks such as 172.18.0.1/32) or "
                "set TRUSTED_PROXY_ENABLED=false."
            ) from exc


async def _start_leader_duties() -> None:
    """Start the analysis engine, housekeeping, notification schedulers, and
    Ogmios ingestion — the leader-only work.

    Ingestion and analysis advance state (the analysis poll watermark, the
    chain-sync checkpoint) that exactly one live process may own; the
    notification schedulers are here because the periodic-report path is
    check-then-act with no atomic claim (get_report_state → dispatch →
    mark_report_sent), so two live schedulers would double-send reports.
    See app.leader.
    """
    global ogmios_client, _leader_duties_started

    # Set FIRST, not last: if startup is cancelled or fails partway through
    # (e.g. shutdown lands mid-promotion), _stop_leader_duties must still
    # unwind whatever did start. Every stop below is idempotent, so stopping
    # never-started duties is harmless; skipping started ones is not.
    _leader_duties_started = True

    if settings.ANALYSIS_ENGINE_ENABLED:
        analysis_task.start()
        logger.info(
            f"Analysis Engine started "
            f"(interval={settings.ANALYSIS_ENGINE_INTERVAL_SECONDS}s, "
            f"batch={settings.ANALYSIS_ENGINE_BATCH_SIZE})"
        )
    # Independent of ANALYSIS_ENGINE_ENABLED: disabling scoring must not also
    # silently disable the stale-PENDING sweep, retention, and auth purge.
    housekeeping_task.start()
    logger.info("Housekeeping task started (interval=%ss)", settings.HOUSEKEEPING_INTERVAL_SECONDS)

    # Periodic-report scheduler + contract_anomaly poller. Self-gates on the
    # `periodic_report.enabled` flag each tick.
    notifications_task.start()
    logger.info("Notification schedulers started")

    from app.ingestion.ogmios_client import OgmiosClient

    ogmios_client = OgmiosClient(on_lifecycle_event=broadcast_lifecycle_event)
    websocket.set_active_connections(active_connections)

    _ingestion_tasks.clear()
    _ingestion_tasks.append(
        asyncio.create_task(_supervised("chain_sync", ogmios_client.run_chain_sync))
    )
    _ingestion_tasks.append(
        asyncio.create_task(_supervised("mempool_monitor", ogmios_client.mempool.run))
    )
    logger.info(f"Ogmios client started for {settings.CARDANO_NETWORK} at {settings.OGMIOS_WS_URL}")


async def _stop_leader_duties() -> None:
    """Undo _start_leader_duties(). No-op if it was never called."""
    global ogmios_client, _leader_duties_started
    if not _leader_duties_started:
        return
    if settings.ANALYSIS_ENGINE_ENABLED:
        analysis_task.stop()
    housekeeping_task.stop()
    notifications_task.stop()
    if ogmios_client:
        await ogmios_client.disconnect()
    # disconnect() signals the supervised coroutines to return; cancel-then-
    # gather so the tasks are actually awaited (not left dangling / GC'd) and a
    # wedged one is force-stopped. return_exceptions keeps one failure from
    # masking the others during shutdown.
    for task in _ingestion_tasks:
        task.cancel()
    if _ingestion_tasks:
        await asyncio.gather(*_ingestion_tasks, return_exceptions=True)
        _ingestion_tasks.clear()
    _leader_duties_started = False


async def _standby_promote() -> None:
    """Retry the leader lock until acquired, then start leader duties.

    Runs only while this instance is a standby (lock held elsewhere at
    startup). Cancelled on shutdown if it never gets promoted.

    Never gives up on an error: a transient Postgres blip during a probe, or
    a failed duty startup after winning the lock, must not leave the fleet
    with a silent permanent standby (probe task dead) or a do-nothing leader
    (lock held, duties not running). On a failed promotion the partial start
    is unwound and the lock released so another instance can win it.
    """
    try:
        while True:
            await asyncio.sleep(settings.LEADER_LOCK_RETRY_SECONDS)
            try:
                if not await leader.try_acquire():
                    continue
            except Exception as e:
                logger.warning("Leader-lock probe failed (%s); retrying", e)
                continue
            logger.info("Leader lock acquired — promoting from standby to leader")
            try:
                await _start_leader_duties()
                return
            except Exception:
                logger.exception(
                    "Promotion failed after acquiring the leader lock; "
                    "unwinding and releasing so another instance can lead"
                )
                await _stop_leader_duties()
                await leader.release()
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown"""
    global ogmios_client, _leader_standby_task

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

        # Notifications: load + validate the stored config at
        # boot (a malformed stored doc fails startup, not the first alert;
        # seeds safe defaults on a fresh DB), capture the event loop for the
        # executor-thread hook, and build the channels. Runs on standbys too
        # so a promotion needs no re-init; the SCHEDULERS (report + poller)
        # are leader-only and start in _start_leader_duties.
        await notifications.load_config()
        notifications.set_main_loop(asyncio.get_running_loop())
        notifications.build_channels()
        logger.info("Notification module ready")

        # Ingestion + analysis: leader-only (see app.leader). Disabled guard =
        # legacy unconditional start (single-instance deploys, current default).
        if settings.LEADER_LOCK_ENABLED:
            if await leader.try_acquire():
                await _start_leader_duties()
            else:
                logger.warning(
                    "Leader lock held by another instance — standing by as a "
                    "read-only replica (retrying every %ss)",
                    settings.LEADER_LOCK_RETRY_SECONDS,
                )
                _leader_standby_task = asyncio.create_task(_standby_promote())
        else:
            await _start_leader_duties()

    except Exception as e:
        logger.error(f"Failed to initialize: {e}")
        raise

    yield

    # Shutdown
    logger.info("Shutting down...")
    stop_all_cleanups()
    if _leader_standby_task and not _leader_standby_task.done():
        _leader_standby_task.cancel()
        await asyncio.gather(_leader_standby_task, return_exceptions=True)
    # Stop scheduling new notification deliveries (the schedulers stop inside
    # _stop_leader_duties; in-flight dispatch tasks finish on their own).
    notifications.set_main_loop(None)
    await _stop_leader_duties()
    await leader.release()
    await postgres.close_pool()
    clickhouse.close_client()
    clickhouse.shutdown_executor()
    if settings.RAW_STORE_ENABLED:
        raw_store.shutdown_executor()
    logger.info("Shutdown complete")


# /docs, /redoc and /openapi.json enumerate the whole admin attack surface
# and sit in the rate-limit exemption list, so they are exposed only in dev
# mode or behind an explicit production opt-in.
from app.auth import _dev_mode as _auth_dev_mode

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
    """,
)

# Middleware registration — Starlette applies middleware in LIFO order,
# so the last registered middleware is the outermost (executes first on request).
#
# Desired execution order (request → response):
#   CORS → CSRF → RateLimiter → Routes
#
# This ensures CORS headers are present on ALL responses, including 429s and
# 403s, and a CSRF-rejected request never consumes a rate-limit slot.

# RateLimiter: registered first → innermost → executes last (closest to routes)
if settings.RATE_LIMIT_ENABLED:
    _limiter = RateLimiter(
        max_requests=settings.RATE_LIMIT_REQUESTS,
        window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
    )
    app.add_middleware(RateLimitMiddleware, limiter=_limiter)

# CSRF double-submit check: defense-in-depth on top of SameSite=Lax. See
# app.csrf module docstring.
app.add_middleware(CSRFMiddleware)

# CORS: registered last → outermost → executes first, wraps everything below.
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
# (produced by the Dockerfile's frontend-build stage), it owns "/"; local
# dev runs the vite dev server (or a one-time `pnpm build`) instead.
FRONTEND_DIST = Path("/app/frontend-dist")
_spa_present = FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file()

app.include_router(websocket.router)

# All REST resources mount under one versioned prefix. Health probes and /ws
# deliberately stay at the root: they are infrastructure surfaces consumed by
# load balancers and the WS handshake, not versioned API resources.
api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(transactions.router)
api_v1.include_router(entities.router)
api_v1.include_router(lifecycle.router)
api_v1.include_router(analysis.router)
api_v1.include_router(archive.router)
api_v1.include_router(auth_api.router)
api_v1.include_router(users_api.router)
api_v1.include_router(clustering_api.router)
api_v1.include_router(notifications_config.router)
api_v1.include_router(backfill.router)
app.include_router(api_v1)


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
        # Lets the SPA show the Validators surfaces only when the module is on.
        "clustering_enabled": settings.CLUSTERING_ENABLED,
    }
    if ogmios_client:
        ogmios_status = ogmios_client.status
        result["ogmios"] = ogmios_status
        result["pipeline_state"] = ogmios_status["pipeline_state"]
    if settings.CLUSTERING_ENABLED:
        result["clustering"] = await _clustering_health()
    return result


async def _clustering_health() -> dict:
    """Liveness of the clustering sidecar via its job heartbeat (best-effort).

    The sidecar's automatic feed updates a job row for every watched contract
    each poll (~30s), so the jobs-table heartbeat advances continuously while
    clustering is healthy. Keying the dot on that heartbeat rather than the last
    published anomaly keeps it green for a healthy-but-quiet contract with no
    recent anomaly: the prior scored_at signal went stale after the freshness
    window even while clustering was actively (and correctly) finding nothing.
    ``absent`` when the jobs table is unreachable/empty (never onboarded, or the
    sidecar is down at first contact); ``stale`` when the heartbeat is older than
    the configured freshness window (feed stopped / sidecar down); else ``ok``.
    """
    from datetime import datetime

    from app.analysis.contract_anomaly import heartbeat_stale_seconds
    from app.db import clustering_queries

    try:
        latest = await clustering_queries.latest_activity_at_async()
    except Exception:
        return {"state": "error", "last_activity_at": None}
    if latest is None:
        return {"state": "absent", "last_activity_at": None}
    window = heartbeat_stale_seconds()
    age = (datetime.now(UTC) - to_aware_utc(latest)).total_seconds()
    state = "stale" if (window and age > window) else "ok"
    return {"state": state, "last_activity_at": str(latest), "age_seconds": round(age)}


# SPA mount goes LAST so /api/*, /health, /ws, etc. still match their routes
# first. Hashed asset filenames are served directly by StaticFiles; any other
# path (client-side router URL like /attacks/123) falls through to index.html.
if _spa_present:
    app.mount(
        "/assets",
        StaticFiles(directory=str(FRONTEND_DIST / "assets")),
        name="spa-assets",
    )

    # Resolve once: the canonical root every served file must stay within.
    _DIST_ROOT = FRONTEND_DIST.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # Starlette's :path convertor matches ".." segments verbatim and does
        # NOT normalise them, so a request like /../../etc/passwd arrives here
        # intact. Resolve the candidate and require it to stay inside the dist
        # root before serving; anything else falls through to index.html so
        # client-side routing still works (no information leak on traversal).
        candidate = (_DIST_ROOT / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(_DIST_ROOT):
            return FileResponse(candidate)
        return FileResponse(_DIST_ROOT / "index.html")
