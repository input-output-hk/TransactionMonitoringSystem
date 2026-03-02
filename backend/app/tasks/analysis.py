"""Background task: runs the Analysis Engine and lifecycle cleanup on a configurable interval."""

import asyncio
import logging

from app.config import settings
from app.analysis import engine
from app.db import postgres

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _loop():
    """Continuously score unanalyzed transactions and drop stale PENDING ones."""
    logger.info(
        f"Analysis Engine background task started "
        f"(network={settings.CARDANO_NETWORK}, "
        f"interval={settings.ANALYSIS_ENGINE_INTERVAL_SECONDS}s, "
        f"batch={settings.ANALYSIS_ENGINE_BATCH_SIZE})"
    )
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
