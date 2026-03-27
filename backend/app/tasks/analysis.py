"""Background task: runs the Analysis Engine and lifecycle cleanup on a configurable interval."""

import asyncio
import logging
import time

from app.config import settings
from app.analysis import engine
from app.analysis import baselines
from app.db import clickhouse
from app.db import postgres

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None

# Timestamp of last baseline recomputation (epoch seconds)
_last_baseline_recompute: float = 0.0


async def _loop():
    """Continuously score unanalyzed transactions and drop stale PENDING ones."""
    global _last_baseline_recompute

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
            scored = await engine.run_once_async(settings.CARDANO_NETWORK)
            if scored == 0:
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

        await asyncio.sleep(settings.ANALYSIS_ENGINE_INTERVAL_SECONDS)


def start():
    """Schedule the analysis loop as a background asyncio task."""
    global _task
    _task = asyncio.create_task(_loop())


def stop():
    """Cancel the background task on shutdown."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None
