"""Background task: cleanup work that must run whether or not scoring does.

The stale-PENDING DROPPED sweep, the retention sweep, and the auth-token /
session purge used to live inside the analysis engine's loop, so disabling
``ANALYSIS_ENGINE_ENABLED`` silently disabled all of them too (review
finding) -- an operator turning off scoring (e.g. during an incident) would
also stop pruning expired auth tokens and stale PENDING transactions with no
indication anything had changed. This loop is independent of that flag.
"""

import asyncio
import logging
import time

from app.config import settings
from app.db import postgres
from app.db import raw_store

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None

# Timestamp of last retention sweep (epoch seconds).
_last_retention_sweep: float = 0.0


async def _tick() -> None:
    """One pass: the stale-PENDING sweep every tick, plus the throttled
    retention + auth-purge sweep. Split out from _loop() so a test can drive
    a single pass without the sleep/while wrapper."""
    global _last_retention_sweep

    # Lifecycle cleanup: mark stale PENDING transactions as DROPPED.
    # Runs on every interval tick; the partial index ensures it is a fast
    # scan even when tx_lifecycle is large.
    try:
        dropped = await postgres.mark_dropped_pending_txs(
            settings.CARDANO_NETWORK,
            settings.LIFECYCLE_PENDING_TTL_SECONDS,
        )
        if dropped > 0:
            logger.info(
                f"Lifecycle cleanup [{settings.CARDANO_NETWORK}]: "
                f"marked {dropped} stale PENDING tx(s) as DROPPED "
                f"(TTL={settings.LIFECYCLE_PENDING_TTL_SECONDS}s)"
            )
    except Exception as e:
        logger.error(f"Lifecycle cleanup error: {e}")

    # Opt-in retention sweep (all knobs default 0 = off), throttled to
    # RETENTION_SWEEP_INTERVAL_HOURS. ClickHouse retention is TTL-based
    # (applied at schema init), so only Postgres and the raw store need
    # an active sweep.
    sweep_interval = settings.RETENTION_SWEEP_INTERVAL_HOURS * 3600
    if time.time() - _last_retention_sweep > sweep_interval:
        _last_retention_sweep = time.time()
        network = settings.CARDANO_NETWORK
        if settings.LIFECYCLE_RETENTION_DAYS > 0:
            try:
                n = await postgres.prune_terminal_lifecycle(
                    network,
                    settings.LIFECYCLE_RETENTION_DAYS,
                )
                if n:
                    logger.info(f"Retention: pruned {n} terminal lifecycle rows")
            except Exception as e:
                logger.error(f"Lifecycle retention sweep failed: {e}")
        if settings.MEMPOOL_COLLISION_RETENTION_DAYS > 0:
            try:
                n = await postgres.prune_mempool_collisions(
                    network,
                    settings.MEMPOOL_COLLISION_RETENTION_DAYS,
                )
                if n:
                    logger.info(f"Retention: pruned {n} mempool collisions")
            except Exception as e:
                logger.error(f"Collision retention sweep failed: {e}")
        if settings.RAW_STORE_RETENTION_DAYS > 0:
            try:
                await asyncio.to_thread(
                    raw_store.prune_old_days,
                    settings.RAW_STORE_RETENTION_DAYS,
                )
            except Exception as e:
                logger.error(f"Raw-store retention sweep failed: {e}")
        if settings.AUDIT_LOG_RETENTION_DAYS > 0:
            try:
                n = await postgres.prune_audit_logs(
                    settings.AUDIT_LOG_RETENTION_DAYS,
                )
                if n:
                    logger.info(f"Retention: pruned {n} audit log rows")
            except Exception as e:
                logger.error(f"Audit-log retention sweep failed: {e}")
        # Auth housekeeping: expired/consumed magic-link tokens and expired
        # sessions accumulate indefinitely otherwise (their purge helpers
        # existed but were never scheduled — review finding). Always swept,
        # NOT gated on a retention knob: these rows are already expired or
        # consumed, so there is no data-retention choice to make.
        try:
            from app.auth.sessions import purge_expired_sessions
            from app.auth.tokens import purge_expired_tokens

            n_tok = await purge_expired_tokens()
            n_sess = await purge_expired_sessions()
            if n_tok or n_sess:
                logger.info(
                    f"Auth purge: removed {n_tok} expired tokens, {n_sess} expired sessions"
                )
        except Exception as e:
            logger.error(f"Auth purge sweep failed: {e}")
        if settings.NOTIFY_DEDUP_RETENTION_DAYS > 0:
            try:
                n = await postgres.prune_notified_alerts(
                    settings.NOTIFY_DEDUP_RETENTION_DAYS,
                )
                if n:
                    logger.info(f"Retention: pruned {n} notified-alert dedup rows")
            except Exception as e:
                logger.error(f"Notified-alerts retention sweep failed: {e}")


async def _loop():
    """Continuously drop stale PENDING transactions and run the retention +
    auth-purge sweeps."""
    logger.info(
        f"Housekeeping background task started "
        f"(network={settings.CARDANO_NETWORK}, "
        f"interval={settings.HOUSEKEEPING_INTERVAL_SECONDS}s)"
    )
    while True:
        await _tick()
        await asyncio.sleep(settings.HOUSEKEEPING_INTERVAL_SECONDS)


def start():
    """Schedule the housekeeping loop as a background asyncio task.

    Idempotent: a second call while the loop runs would leak the first task
    and run two concurrent sweeps (harmless — every operation here is an
    idempotent DELETE/UPDATE — but wasted work).
    """
    global _task
    if _task is not None and not _task.done():
        logger.warning("Housekeeping loop already running; start() ignored")
        return
    _task = asyncio.create_task(_loop())


def stop():
    """Cancel the background task on shutdown."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None
