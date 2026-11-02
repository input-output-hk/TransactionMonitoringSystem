"""Background task: runs the Analysis Engine on a configurable interval.

Two loops drive the engine's two lanes (see app.analysis.engine): _loop scores
the live window, _backlog_loop scores what left it unscored and keeps the
scoring checkpoint. scoring_status() reports both for /health/detail.

Cleanup work that must run independently of scoring (stale-PENDING sweep,
retention, auth purge) lives in app.tasks.housekeeping instead — see its
module docstring.
"""

import asyncio
import functools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.analysis import baselines, engine, external
from app.config import settings
from app.db import clickhouse, postgres
from app.utils.datetime_utils import format_iso_utc

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_backlog_task: asyncio.Task | None = None
# Stop signal of the lanes' batches: a batch still on its executor thread when
# stop() runs stores nothing (engine._score_claimed), so its rows are picked
# again after the restart instead of being stored with what a stopped event
# loop could not supply (collision enrichment, alerts). One per start, for the
# reason given at _baseline_stop.
_lane_stop: threading.Event | None = None


@dataclass
class _ScoringState:
    """What the two lanes last saw, for scoring_status()."""

    # When the live lane started, and when it last finished a poll.
    live_started_at: datetime | None = None
    live_polled_at: datetime | None = None
    # Every transaction ingested before this has a score (scoring_checkpoint).
    checkpoint: datetime | None = None
    # Ingestion stamp of the oldest transaction the backlog lane last found
    # waiting; None once a tick reached the live window with nothing waiting.
    oldest_unscored: datetime | None = None


_scoring = _ScoringState()
# The state last logged, so a change is logged once rather than every tick.
_logged_state: str | None = None
# Logged at ERROR: something is wrong. The unguarded states are an operator's
# choice, logged once at WARNING since nothing then reports what they miss.
_BAD_STATES = ("behind", "stalled")
_UNGUARDED_STATES = ("disabled", "unguarded")

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
            # full, up to a per-tick cap so a burst cannot starve the other
            # duties below (and the shared ClickHouse executor). What the cap
            # leaves behind either fits the next tick or ages out of the live
            # window into the backlog lane.
            history_complete = not _history_incomplete(datetime.now(UTC))
            batches = 0
            while batches < settings.ANALYSIS_ENGINE_MAX_BATCHES_PER_TICK:
                processed = await engine.run_once_async(
                    settings.CARDANO_NETWORK, history_complete, _should_stop()
                )
                batches += 1
                # Per poll, not per tick: a saturated tick of slow batches
                # outlasts the stall tolerance while every poll succeeds.
                _scoring.live_polled_at = datetime.now(UTC)
                if processed < settings.ANALYSIS_ENGINE_BATCH_SIZE:
                    break
                await asyncio.sleep(settings.ANALYSIS_ENGINE_DRAIN_SLEEP_SECONDS)
            if batches == 1 and processed == 0:
                logger.debug("Analysis Engine: no new transactions to score")
            # Stopped by the cap with its last poll still full: what it left
            # ages out of the live window into the backlog lane, where a
            # late score costs far more, so the next tick starts at once.
            saturated = processed >= settings.ANALYSIS_ENGINE_BATCH_SIZE
        except Exception as e:
            saturated = False
            logger.error("Analysis Engine error: %s", e)
        _log_scoring_state()

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

        await asyncio.sleep(
            settings.ANALYSIS_ENGINE_DRAIN_SLEEP_SECONDS
            if saturated
            else settings.ANALYSIS_ENGINE_INTERVAL_SECONDS
        )


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


async def _backlog_loop() -> None:
    """Score what left the live window unscored, oldest first, forever."""
    logger.info(
        "Analysis Engine backlog lane started (slice=%ss, pick=%s, batch=%s, max_threads=%s)",
        settings.ANALYSIS_BACKLOG_SLICE_SECONDS,
        settings.ANALYSIS_BACKLOG_PICK_SIZE,
        settings.ANALYSIS_BACKLOG_BATCH_SIZE,
        settings.ANALYSIS_BACKLOG_MAX_THREADS,
    )
    while True:
        try:
            await _backlog_tick(settings.CARDANO_NETWORK)
        except Exception as e:
            logger.error("Analysis Engine backlog lane error: %s", e)
        _log_scoring_state()
        await asyncio.sleep(settings.ANALYSIS_ENGINE_INTERVAL_SECONDS)


async def _backlog_tick(network: str) -> None:
    """Walk from the checkpoint toward the live window one slice at a time.

    A slice found clean moves the checkpoint past it, up to the settle margin
    (ANALYSIS_BACKLOG_SETTLE_SECONDS); a slice with work is drained oldest
    first, and the tick ends at the first slice it cannot finish (a deferred
    retry, or a transaction the live lane holds), so the checkpoint never
    passes an unscored transaction. Slices are only ever entered in order.

    It runs back to back up to where the live window started when the tick
    began: its executor is its own and its queries are thread-capped, so a
    drain of any size costs at most those threads and never waits on a
    budget. The target is fixed for the tick because the window keeps moving:
    chasing it would pick an ever thinner slice at its edge, forever. What
    ages out meanwhile is the next tick's.

    While the walk is behind the recent region (ANALYSIS_BACKLOG_RECENT_SECONDS)
    it yields to it at the start of the tick and then every interval between
    batches (_RecentPass), so a transaction that has just left the live window
    is not scored after all the history older than it.
    """
    if not settings.ANALYSIS_ENABLED:
        # Picks would come back empty because nothing scores, not because
        # nothing waits: moving the checkpoint over them would leave every
        # transaction of the pause behind it once scoring resumes.
        return
    window_start = datetime.now(UTC) - timedelta(seconds=settings.ANALYSIS_LIVE_WINDOW_SECONDS)
    settled = window_start - timedelta(seconds=settings.ANALYSIS_BACKLOG_SETTLE_SECONDS)
    if _scoring.checkpoint is None:
        _scoring.checkpoint = await _initial_checkpoint(network, settled)
        if _scoring.checkpoint is None:
            return  # nothing ingested yet
    if _scoring.checkpoint > settled:
        await _rewind_checkpoint(network, settled)
    slice_span = timedelta(seconds=settings.ANALYSIS_BACKLOG_SLICE_SECONDS)
    # A tail up to half a slice is picked with the slice before it: on its own
    # it costs a full pick (see pick_unanalyzed) and, caught up, lies past the
    # settle margin, where it cannot move the checkpoint. Caught up, the tail
    # is what the clock moved since the last tick (its interval and its run),
    # so it is always longer than the interval alone. The pick's memory follows
    # its range, hence half a slice and no more.
    tail = slice_span / 2
    pos = _scoring.checkpoint
    recent = _RecentPass(network)
    await recent.run(walk_at=pos, after=min(pos + slice_span, window_start))
    just_scored: set[str] = set()
    while pos < window_start:
        end = pos + slice_span
        if end + tail >= window_start:
            end = window_start
        picked = await engine.pick_backlog_async(network, pos, end)
        if not picked:
            # Clean, and so is everything before it.
            await _advance_checkpoint(network, min(end, settled))
            pos = end
            just_scored.clear()
            continue
        if just_scored.intersection(tx_hash for tx_hash, _ in picked):
            # Scored a moment ago, yet still unscored to the pick: picking
            # again would score it again forever.
            logger.error(
                "Backlog lane: transactions scored in [%s, %s) are still picked as "
                "unscored; stopping until the next tick",
                format_iso_utc(pos),
                format_iso_utc(end),
            )
            return
        _scoring.oldest_unscored = picked[0][1]
        if not await _score_picked(network, picked, pos, end, recent):
            # Some of the slice waits on a retry or on the live lane.
            return
        # Pick the slice again: it holds more, or is now clean.
        just_scored = {tx_hash for tx_hash, _ in picked}
    _scoring.oldest_unscored = None


async def _score_picked(
    network: str,
    picked: list[tuple[str, datetime]],
    since: datetime,
    until: datetime,
    recent: "_RecentPass | None" = None,
) -> bool:
    """Score picked transactions a batch at a time; True when every one now has a score.

    Between batches of the historical walk, ``recent`` gets its turn once an
    interval has passed since its last one.
    """
    batch = settings.ANALYSIS_BACKLOG_BATCH_SIZE
    complete = True
    for start in range(0, len(picked), batch):
        hashes = [tx_hash for tx_hash, _ in picked[start : start + batch]]
        scored = await engine.run_backlog_once_async(network, hashes, since, until, _should_stop())
        complete = complete and scored.scored == len(hashes)
        if recent is not None and recent.due():
            await recent.run(walk_at=since, after=until)
    return complete


class _RecentPass:
    """The backlog lane's turn at what has just left the live window.

    Without it a transaction that ages out of the live window unscored (a burst
    the live lane cannot take, or one an adversary sends to push a transaction
    out) waits behind every older unscored one: months, while a large backlog
    drains. Oldest first within the region, so a burst sent after a
    transaction queues behind it. It never moves the checkpoint, so the walk
    still checks every slice it scored here (finding them clean costs a pick).
    It starts after the slice the walk is in, which that walk scores itself.
    """

    def __init__(self, network: str) -> None:
        self.network = network
        self.last = time.monotonic()
        # The hashes of its last pick: picked again, they were scored and are
        # still not visible as scored, and picking again would loop.
        self.scored: set[str] = set()
        self.stuck = False

    def due(self) -> bool:
        return time.monotonic() - self.last >= settings.ANALYSIS_ENGINE_INTERVAL_SECONDS

    async def run(self, walk_at: datetime, after: datetime) -> None:
        """One pick of the region after ``after``, scored, unless the walk at ``walk_at`` is in it."""
        self.last = time.monotonic()
        span = settings.ANALYSIS_BACKLOG_RECENT_SECONDS
        if not span or self.stuck:
            return
        window_start = datetime.now(UTC) - timedelta(seconds=settings.ANALYSIS_LIVE_WINDOW_SECONDS)
        region_start = window_start - timedelta(seconds=span)
        if walk_at >= region_start:
            return  # the walk is in the region already and scores it in order
        since = max(after, region_start)
        if since >= window_start:
            return
        picked = await engine.pick_backlog_async(self.network, since, window_start)
        if not picked:
            return
        if self.scored.intersection(tx_hash for tx_hash, _ in picked):
            logger.error(
                "Backlog lane: transactions scored in the recent region [%s, %s) are still "
                "picked as unscored; leaving the region to the walk until the next tick",
                format_iso_utc(since),
                format_iso_utc(window_start),
            )
            self.stuck = True
            return
        self.scored = {tx_hash for tx_hash, _ in picked}
        batch = settings.ANALYSIS_BACKLOG_BATCH_SIZE
        for start in range(0, len(picked), batch):
            hashes = [tx_hash for tx_hash, _ in picked[start : start + batch]]
            await engine.run_backlog_once_async(
                self.network, hashes, since, window_start, _should_stop()
            )
        self.last = time.monotonic()


async def _initial_checkpoint(network: str, settled: datetime) -> datetime | None:
    """The saved checkpoint, else the first ingestion on record (a full first pass).

    A first ingestion past the settle margin (a new install) is saved at the
    margin instead: the checkpoint is never saved ahead of it, which is what
    lets _rewind_checkpoint read one that is as a clock step.
    """
    saved = await postgres.get_scoring_checkpoint(network)
    if saved is not None:
        return saved
    loop = asyncio.get_running_loop()
    earliest = await loop.run_in_executor(
        clickhouse._ch_backlog_executor, clickhouse.earliest_ingestion, network
    )
    if earliest is not None:
        logger.info(
            "No scoring checkpoint for %s: the backlog lane checks everything ingested since %s",
            network,
            format_iso_utc(earliest),
        )
        earliest = min(earliest, settled)
        await postgres.save_scoring_checkpoint(network, earliest)
    return earliest


async def _advance_checkpoint(network: str, target: datetime) -> None:
    if _scoring.checkpoint is not None and target <= _scoring.checkpoint:
        return
    await postgres.save_scoring_checkpoint(network, target)
    _scoring.checkpoint = target


async def _rewind_checkpoint(network: str, settled: datetime) -> None:
    """Bring a checkpoint ahead of the settle margin back to it.

    The checkpoint is only ever saved at or behind the settle margin of its
    time, so one ahead of it means the host clock stepped back (or differs
    from the previous leader's), or ANALYSIS_BACKLOG_SETTLE_SECONDS was
    raised. Left there, every transaction stamped behind it from then on
    would be scored only if the live lane got to it. Rewinding only re-checks
    slices.
    """
    logger.warning(
        "Scoring checkpoint %s is ahead of its settle margin %s: the host clock stepped "
        "back, or the settle margin was raised; rewinding it",
        format_iso_utc(_scoring.checkpoint),
        format_iso_utc(settled),
    )
    await postgres.rewind_scoring_checkpoint(network, settled)
    _scoring.checkpoint = settled


def _should_stop() -> Callable[[], bool] | None:
    """The stop test this start's batches check (see _lane_stop)."""
    return _lane_stop.is_set if _lane_stop is not None else None


def _history_incomplete(now: datetime) -> bool:
    """Whether a transaction older than the live window may still be unscored.

    True when the backlog lane found one waiting longer than the tolerance
    allows, or its checkpoint trails the settle margin by more than the
    tolerance (as during a first pass, before it has checked the history).
    Never True with the backlog lane off: nothing would then know.
    """
    if not settings.ANALYSIS_BACKLOG_ENABLED:
        return False
    tolerance = timedelta(seconds=settings.ANALYSIS_SCORING_LAG_TOLERANCE_SECONDS)
    window_start = now - timedelta(seconds=settings.ANALYSIS_LIVE_WINDOW_SECONDS)
    if _scoring.oldest_unscored is not None and _scoring.oldest_unscored < window_start - tolerance:
        return True
    settled = window_start - timedelta(seconds=settings.ANALYSIS_BACKLOG_SETTLE_SECONDS)
    return _scoring.checkpoint is None or _scoring.checkpoint < settled - tolerance


def _retention_at_risk(now: datetime) -> bool:
    """Whether unscored rows may expire before the backlog lane reaches them.

    True once the checkpoint is older than ANALYSIS_RETENTION_WARNING_SHARE of
    the shortest positive row retention. Nothing records a row that expires
    unscored, so this is the only warning there is.
    """
    days = [
        d for d in (settings.CH_RETENTION_DAYS_TRANSACTIONS, settings.CH_RETENTION_DAYS_IO) if d > 0
    ]
    if not days or _scoring.checkpoint is None:
        return False
    limit = timedelta(days=min(days)) * settings.ANALYSIS_RETENTION_WARNING_SHARE
    return now - _scoring.checkpoint > limit


def scoring_status() -> dict[str, Any]:
    """The scoring lanes' health for /health/detail.

    ``state`` is "disabled" when ANALYSIS_ENABLED is off (nothing scores),
    "starting" until the live lane finishes its first poll, "stalled" when it
    has not finished one (or, since it started, its first) for the tolerance
    past its interval, "behind" when transactions older than the live window
    may be unscored (see _history_incomplete) or rows behind the checkpoint
    near their retention (see _retention_at_risk), "unguarded" when all is
    well but the backlog lane is off (so nothing would score, or know of, a
    transaction the live lane misses), and "ok" otherwise.
    """
    now = datetime.now(UTC)
    stall_after = timedelta(
        seconds=settings.ANALYSIS_ENGINE_INTERVAL_SECONDS
        + settings.ANALYSIS_SCORING_LAG_TOLERANCE_SECONDS
    )
    retention_at_risk = _retention_at_risk(now)
    last_progress = _scoring.live_polled_at or _scoring.live_started_at
    if not settings.ANALYSIS_ENABLED:
        state = "disabled"
    elif last_progress is not None and now - last_progress > stall_after:
        state = "stalled"
    elif _scoring.live_polled_at is None:
        state = "starting"
    elif retention_at_risk or _history_incomplete(now):
        state = "behind"
    elif not settings.ANALYSIS_BACKLOG_ENABLED:
        state = "unguarded"
    else:
        state = "ok"
    return {
        "state": state,
        "retention_at_risk": retention_at_risk,
        "live_window_seconds": settings.ANALYSIS_LIVE_WINDOW_SECONDS,
        "live_polled_at": format_iso_utc(_scoring.live_polled_at),
        "backlog_enabled": settings.ANALYSIS_BACKLOG_ENABLED,
        "checkpoint": format_iso_utc(_scoring.checkpoint),
        "oldest_unscored_at": format_iso_utc(_scoring.oldest_unscored),
    }


def _log_scoring_state() -> None:
    """Log a change of scoring state once: an error going bad, info recovering."""
    global _logged_state
    status = scoring_status()
    # The retention risk is its own transition: it usually comes while the
    # state already reads "behind", which would otherwise log nothing new.
    state = status["state"]
    if status["retention_at_risk"]:
        state = f"{state}, retention at risk"
    if state == _logged_state:
        return
    if status["state"] in _BAD_STATES:
        logger.error("Scoring is %s: %s", state, status)
    elif status["state"] in _UNGUARDED_STATES:
        logger.warning("Scoring is %s: %s", state, status)
    elif _logged_state is not None and _logged_state.startswith(_BAD_STATES):
        logger.info("Scoring recovered (%s): %s", state, status)
    _logged_state = state


def start():
    """Schedule the analysis loops as background asyncio tasks.

    Idempotent: a second call while the loops run would leak the first tasks
    and run two drain loops per lane (duplicate scoring is RMT-absorbed, but
    the wasted work and a second checkpoint writer are not worth it).
    """
    global _task, _backlog_task, _lane_stop
    if _task is not None and not _task.done():
        logger.warning("Analysis loop already running; start() ignored")
        return
    # A fresh start: the checkpoint is re-read (another leader may have moved
    # it meanwhile), and a previous leadership's last poll is not this one's.
    _scoring.checkpoint = None
    _scoring.oldest_unscored = None
    _scoring.live_polled_at = None
    _scoring.live_started_at = datetime.now(UTC)
    _lane_stop = threading.Event()
    _task = asyncio.create_task(_loop())
    if settings.ANALYSIS_BACKLOG_ENABLED:
        _backlog_task = asyncio.create_task(_backlog_loop())
    else:
        logger.warning(
            "Analysis Engine backlog lane is off: a transaction the live lane does not "
            "score within its window is never scored"
        )


def stop():
    """Cancel the background tasks on shutdown."""
    global _task, _backlog_task, _baseline_task
    # Ends an in-flight recompute at its next scope: cancelling the awaiting
    # task alone would leave the executor thread scanning to the end.
    if _baseline_stop is not None:
        _baseline_stop.set()
    if _lane_stop is not None:
        _lane_stop.set()
    if _baseline_task and not _baseline_task.done():
        _baseline_task.cancel()
    _baseline_task = None
    # Each lane stops between batches: the one in flight, if any, finishes on
    # its thread without storing (see _lane_stop).
    if _backlog_task and not _backlog_task.done():
        _backlog_task.cancel()
    _backlog_task = None
    if _task and not _task.done():
        _task.cancel()
        _task = None
