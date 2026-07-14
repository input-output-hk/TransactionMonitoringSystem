"""Background task: runs the Analysis Engine on a configurable interval.

Cleanup work that must run independently of scoring (stale-PENDING sweep,
retention, auth purge) lives in app.tasks.housekeeping instead — see its
module docstring.
"""

import asyncio
import logging
import time

from app.analysis import baselines, engine, external
from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None

# Timestamp of last baseline recomputation (epoch seconds)
_last_baseline_recompute: float = 0.0

# Timestamp of last token-registry refresh (epoch seconds). 0.0 forces a
# refresh on the first tick so fake_token starts with full registry coverage
# instead of the seed list.
_last_registry_refresh: float = 0.0


async def _loop():
    """Continuously score unanalyzed transactions."""
    global _last_baseline_recompute, _last_registry_refresh

    logger.info(
        f"Analysis Engine background task started "
        f"(network={settings.CARDANO_NETWORK}, "
        f"interval={settings.ANALYSIS_ENGINE_INTERVAL_SECONDS}s, "
        f"batch={settings.ANALYSIS_ENGINE_BATCH_SIZE})"
    )

    # Bootstrap baselines on first run if enabled and table is empty. Runs on
    # the ClickHouse executor, NOT inline: bootstrap_baselines is a synchronous
    # warehouse scan, and awaiting it inline here would block the event loop
    # (which also serves the API, WebSocket feed, and ingestion) for the whole
    # scan on the first mainnet boot, where it is the slowest.
    if settings.BASELINE_BOOTSTRAP_ON_STARTUP:
        try:
            loop = asyncio.get_running_loop()
            count = await loop.run_in_executor(
                clickhouse._ch_executor,
                baselines.bootstrap_baselines,
                settings.CARDANO_NETWORK,
            )
            if count > 0:
                logger.info("Baseline bootstrap: created %s baseline rows", count)
        except Exception as e:
            logger.error("Baseline bootstrap failed (non-fatal): %s", e)

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
            logger.error("Analysis Engine error: %s", e)

        # Periodic token-registry refresh. The fetch never runs on the
        # scoring path (external.get_legitimate_tokens serves the cache,
        # stale included); this is the only place it happens, on the default
        # thread-pool executor so the blocking HTTP work occupies neither
        # the event loop nor the ClickHouse workers.
        registry_needed = settings.SCORER_FAKE_TOKEN_ENABLED and (
            settings.CARDANO_NETWORK == "mainnet" or settings.FAKE_TOKEN_TESTNET_MODE
        )
        refresh_interval = settings.TOKEN_REGISTRY_REFRESH_INTERVAL_HOURS * 3600
        if registry_needed and time.time() - _last_registry_refresh > refresh_interval:
            try:
                count = await asyncio.to_thread(external.refresh_token_registry)
                _last_registry_refresh = time.time()
                logger.info("Token registry refreshed: %s names", count)
            except Exception as e:
                logger.error("Token registry refresh failed (serving cache/seeds): %s", e)
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
                    logger.info("Baseline recomputation: %s rows updated", total)
            except Exception as e:
                logger.error("Baseline recomputation failed: %s", e)

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
