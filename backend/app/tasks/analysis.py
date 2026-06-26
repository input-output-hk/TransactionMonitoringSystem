"""Background task: runs the Analysis Engine and lifecycle cleanup on a configurable interval."""

import asyncio
import logging
import time

from app.config import settings
from app.analysis import engine
from app.analysis import baselines
from app.analysis import external
from app.db import clickhouse
from app.db import postgres
from app.db import raw_store

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None

# Timestamp of last baseline recomputation (epoch seconds)
_last_baseline_recompute: float = 0.0

# Timestamp of last token-registry refresh (epoch seconds). 0.0 forces a
# refresh on the first tick so fake_token starts with full registry coverage
# instead of the seed list.
_last_registry_refresh: float = 0.0

# Timestamp of last retention sweep (epoch seconds).
_last_retention_sweep: float = 0.0


async def _loop():
    """Continuously score unanalyzed transactions and drop stale PENDING ones."""
    global _last_baseline_recompute, _last_registry_refresh, _last_retention_sweep

    logger.info(
        f"Analysis Engine background task started "
        f"(network={settings.CARDANO_NETWORK}, "
        f"interval={settings.ANALYSIS_ENGINE_INTERVAL_SECONDS}s, "
        f"batch={settings.ANALYSIS_ENGINE_BATCH_SIZE})"
    )

    # Bootstrap baselines on first run if enabled and table is empty
    if settings.BASELINE_BOOTSTRAP_ON_STARTUP:
        try:
            count = baselines.bootstrap_baselines(settings.CARDANO_NETWORK)
            if count > 0:
                logger.info(f"Baseline bootstrap: created {count} baseline rows")
        except Exception as e:
            logger.error(f"Baseline bootstrap failed (non-fatal): {e}")

    while True:
        try:
            # Drain loop: keep pulling batches while the poll comes back
            # full, up to a per-tick cap so a deep backlog cannot starve
            # the other duties below (and the shared ClickHouse executor).
            # Previously one fixed batch per interval capped throughput at
            # BATCH_SIZE / INTERVAL regardless of backlog depth.
            batches = 0
            while batches < settings.ANALYSIS_ENGINE_MAX_BATCHES_PER_TICK:
                processed = await engine.run_once_async(settings.CARDANO_NETWORK)
                batches += 1
                if processed < settings.ANALYSIS_ENGINE_BATCH_SIZE:
                    break
                await asyncio.sleep(settings.ANALYSIS_ENGINE_DRAIN_SLEEP_SECONDS)
            if batches == 1 and processed == 0:
                logger.debug("Analysis Engine: no new transactions to score")
        except Exception as e:
            logger.error(f"Analysis Engine error: {e}")

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

        # Periodic token-registry refresh. The fetch never runs on the
        # scoring path (external.get_legitimate_tokens serves the cache,
        # stale included); this is the only place it happens, on the default
        # thread-pool executor so the blocking HTTP work occupies neither
        # the event loop nor the ClickHouse workers.
        registry_needed = settings.SCORER_FAKE_TOKEN_ENABLED and (
            settings.CARDANO_NETWORK == "mainnet"
            or settings.FAKE_TOKEN_TESTNET_MODE
        )
        refresh_interval = settings.TOKEN_REGISTRY_REFRESH_INTERVAL_HOURS * 3600
        if registry_needed and time.time() - _last_registry_refresh > refresh_interval:
            try:
                count = await asyncio.to_thread(external.refresh_token_registry)
                _last_registry_refresh = time.time()
                logger.info(f"Token registry refreshed: {count} names")
            except Exception as e:
                logger.error(f"Token registry refresh failed (serving cache/seeds): {e}")
                # Back off a full interval on failure too; the scorer keeps
                # serving the previous cache or the seed list meanwhile.
                _last_registry_refresh = time.time()

        # Periodic baseline recomputation
        recompute_interval = settings.BASELINE_RECOMPUTE_INTERVAL_HOURS * 3600
        if time.time() - _last_baseline_recompute > recompute_interval:
            try:
                loop = asyncio.get_running_loop()
                total = await loop.run_in_executor(
                    clickhouse._ch_executor,
                    baselines.recompute_all_baselines,
                    settings.CARDANO_NETWORK,
                    settings.BASELINE_MAX_SCRIPTS,
                )
                _last_baseline_recompute = time.time()
                if total > 0:
                    logger.info(f"Baseline recomputation: {total} rows updated")
            except Exception as e:
                logger.error(f"Baseline recomputation failed: {e}")

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
                        network, settings.LIFECYCLE_RETENTION_DAYS,
                    )
                    if n:
                        logger.info(f"Retention: pruned {n} terminal lifecycle rows")
                except Exception as e:
                    logger.error(f"Lifecycle retention sweep failed: {e}")
            if settings.MEMPOOL_COLLISION_RETENTION_DAYS > 0:
                try:
                    n = await postgres.prune_mempool_collisions(
                        network, settings.MEMPOOL_COLLISION_RETENTION_DAYS,
                    )
                    if n:
                        logger.info(f"Retention: pruned {n} mempool collisions")
                except Exception as e:
                    logger.error(f"Collision retention sweep failed: {e}")
            if settings.RAW_STORE_RETENTION_DAYS > 0:
                try:
                    await asyncio.to_thread(
                        raw_store.prune_old_days, settings.RAW_STORE_RETENTION_DAYS,
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
                        f"Auth purge: removed {n_tok} expired tokens, "
                        f"{n_sess} expired sessions"
                    )
            except Exception as e:
                logger.error(f"Auth purge sweep failed: {e}")

        await asyncio.sleep(settings.ANALYSIS_ENGINE_INTERVAL_SECONDS)


def start():
    """Schedule the analysis loop as a background asyncio task.

    Idempotent: a second call while the loop runs would leak the first
    task and run two concurrent drain loops mutating the watermark from
    two executor threads (duplicate scoring is RMT-absorbed, but the
    wasted work and interleaved cursors are not worth it).
    """
    global _task
    if _task is not None and not _task.done():
        logger.warning("Analysis loop already running; start() ignored")
        return
    _task = asyncio.create_task(_loop())


def stop():
    """Cancel the background task on shutdown."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None
