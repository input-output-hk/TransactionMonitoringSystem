"""Background task: runs the Analysis Engine on a configurable interval.

Cleanup work that must run independently of scoring (stale-PENDING sweep,
retention, auth purge) lives in app.tasks.housekeeping instead — see its
module docstring.
"""

import asyncio
import functools
import logging
import threading
import time

from app.analysis import baselines, engine, external
from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None

_SECONDS_PER_HOUR = 3600

# Timestamp of last baseline recomputation (epoch seconds). The loop seeds it
# from the store when it starts (see _initial_baseline_schedule); 0.0 = due now.
_last_baseline_recompute: float = 0.0
# A failed recompute is not retried before this epoch (see
# BASELINE_RECOMPUTE_RETRY_SECONDS).
_baseline_retry_not_before: float = 0.0
# The recompute in flight, if any. It runs beside the scoring loop, never inside
# it: awaited inline, it stopped all scoring for its whole run (~17 minutes on
# mainnet, daily and on every restart).
_baseline_task: asyncio.Task | None = None
# Stop signal of the recompute in flight: stop() sets it so the run returns
# after its current scope instead of running out a scan that can take up to an
# hour. One per run, never cleared: a module-wide flag that start() cleared
# could be un-set under a scan still running from before a leader hand-back.
_baseline_stop: threading.Event | None = None

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

    # max(): on a leader hand-back inside one process, the in-memory time can be
    # newer than anything the store has caught up with.
    _last_baseline_recompute = max(_last_baseline_recompute, await _initial_baseline_schedule())

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
        refresh_interval = settings.TOKEN_REGISTRY_REFRESH_INTERVAL_HOURS * _SECONDS_PER_HOUR
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

        # Periodic baseline recomputation, started beside the loop (not awaited)
        _maybe_start_baseline_recompute()

        await asyncio.sleep(settings.ANALYSIS_ENGINE_INTERVAL_SECONDS)


async def _initial_baseline_schedule() -> float:
    """Where the baseline schedule starts when the loop does.

    0.0 (recompute now) when BASELINE_RECOMPUTE_ON_STARTUP asks for it, when no
    full recompute is on record, or when the store cannot be read: a missing
    reading must never postpone a recompute. Otherwise the time the last full
    recompute landed, so a restart resumes the daily schedule instead of
    re-running a recompute that finished hours ago.
    """
    if settings.BASELINE_RECOMPUTE_ON_STARTUP:
        return 0.0
    try:
        return await clickhouse._in_executor(
            clickhouse.latest_full_baseline_recompute, settings.CARDANO_NETWORK
        )
    except Exception as e:
        logger.warning("Baseline schedule unreadable, recomputing now: %s", e)
        return 0.0


def _maybe_start_baseline_recompute() -> None:
    """Start a baseline recompute beside the scoring loop if one is due.

    At most one runs at a time, and a failed run waits out its retry backoff.
    """
    global _baseline_task, _baseline_stop
    if _baseline_task is not None and not _baseline_task.done():
        return
    now = time.time()
    if now < _baseline_retry_not_before:
        return
    if (
        now - _last_baseline_recompute
        <= settings.BASELINE_RECOMPUTE_INTERVAL_HOURS * _SECONDS_PER_HOUR
    ):
        return
    _baseline_stop = threading.Event()
    _baseline_task = asyncio.create_task(_run_baseline_recompute(_baseline_stop))


async def _run_baseline_recompute(stop_event: threading.Event) -> None:
    """One recompute on the maintenance executor, recording its outcome."""
    global _last_baseline_recompute, _baseline_retry_not_before
    loop = asyncio.get_running_loop()
    try:
        total = await loop.run_in_executor(
            clickhouse._ch_maintenance_executor,
            functools.partial(
                baselines.recompute_all_baselines,
                settings.CARDANO_NETWORK,
                settings.BASELINE_MAX_SCRIPTS,
                should_stop=stop_event.is_set,
            ),
        )
    except Exception as e:
        _baseline_retry_not_before = time.time() + settings.BASELINE_RECOMPUTE_RETRY_SECONDS
        logger.error(
            "Baseline recomputation failed, retrying in %ss: %s",
            settings.BASELINE_RECOMPUTE_RETRY_SECONDS,
            e,
        )
        return
    _last_baseline_recompute = time.time()
    if total > 0:
        logger.info("Baseline recomputation: %s rows updated", total)


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
    global _task, _baseline_task
    # Ends an in-flight recompute at its next scope: cancelling the awaiting
    # task alone would leave the executor thread scanning to the end.
    if _baseline_stop is not None:
        _baseline_stop.set()
    if _baseline_task and not _baseline_task.done():
        _baseline_task.cancel()
    _baseline_task = None
    if _task and not _task.done():
        _task.cancel()
        _task = None
