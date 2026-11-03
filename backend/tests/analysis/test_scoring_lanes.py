"""The scoring lanes' driver: the backlog lane's checkpoint, and scoring health.

The checkpoint is the never-skip guarantee: every transaction ingested before it
has a score. These tests drive _backlog_tick against a fake backlog that picks
and scores like the real lane (oldest first, a batch at a time) and hold it to
the one rule that matters for recall: the checkpoint never passes an unscored
transaction.
"""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.analysis import engine
from app.analysis.engine import BacklogBatch
from app.config import settings
from app.tasks import analysis

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
NETWORK = "preprod"

# Test values for the lane knobs (set in the fixture below).
_WINDOW = timedelta(minutes=10)
_SETTLE = timedelta(hours=1)
_SLICE = timedelta(days=1)
_TOLERANCE = timedelta(minutes=10)
_PICK = 50
_BATCH = 20
_INTERVAL_SECONDS = 30
_REGION = timedelta(days=1)
# Where the history these tests drain sits: far behind the recent region.
_HISTORY_HOURS_BACK = 72
_RETENTION_DAYS = 30

# Where a fully caught-up checkpoint sits: the settle margin behind the live window.
_SETTLED = NOW - _WINDOW - _SETTLE
# Event-loop turns that let freshly started tasks reach their first await.
_GATE_YIELDS = 10


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class _FakeBacklog:
    """Unscored transactions by ingestion stamp, picked and scored like the lane.

    A transaction whose stamp is in ``held`` is picked every time but never
    scored: a deferred retry, or one the live lane holds.
    """

    def __init__(self, stamps=(), held=()):
        self.unscored = {f"tx{i:06d}": stamp for i, stamp in enumerate(stamps)}
        self.held = set(held)
        self.picks: list[tuple[datetime, datetime]] = []
        self.batches: list[list[str]] = []

    def stamps(self) -> list[datetime]:
        return sorted(self.unscored.values())

    async def pick(self, network, since, until):
        self.picks.append((since, until))
        found = sorted((s, h) for h, s in self.unscored.items() if since <= s < until)
        return [(h, s) for s, h in found[: settings.ANALYSIS_BACKLOG_PICK_SIZE]]

    async def score(self, network, hashes, since, until, should_stop=None):
        self.batches.append(list(hashes))
        found = [h for h in hashes if h in self.unscored]
        scored = [h for h in found if self.unscored[h] not in self.held]
        for tx_hash in scored:
            del self.unscored[tx_hash]
        return BacklogBatch(len(found), len(scored))


@pytest.fixture
def saved(monkeypatch):
    """The persisted checkpoint, as save_scoring_checkpoint leaves it."""
    store: dict[str, datetime] = {}

    async def _save(network, complete_through):
        store[network] = max(store.get(network, complete_through), complete_through)

    async def _rewind(network, complete_through):
        if network in store:
            store[network] = min(store[network], complete_through)

    monkeypatch.setattr(analysis.postgres, "save_scoring_checkpoint", _save)
    monkeypatch.setattr(analysis.postgres, "rewind_scoring_checkpoint", _rewind)
    monkeypatch.setattr(
        analysis.postgres, "get_scoring_checkpoint", AsyncMock(side_effect=lambda n: store.get(n))
    )
    return store


@pytest.fixture(autouse=True)
def _lanes(monkeypatch, saved):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)
    monkeypatch.setattr(settings, "ANALYSIS_BACKLOG_ENABLED", True)
    monkeypatch.setattr(settings, "ANALYSIS_LIVE_WINDOW_SECONDS", int(_WINDOW.total_seconds()))
    monkeypatch.setattr(settings, "ANALYSIS_BACKLOG_SETTLE_SECONDS", int(_SETTLE.total_seconds()))
    monkeypatch.setattr(settings, "ANALYSIS_BACKLOG_SLICE_SECONDS", int(_SLICE.total_seconds()))
    monkeypatch.setattr(settings, "ANALYSIS_BACKLOG_PICK_SIZE", _PICK)
    monkeypatch.setattr(settings, "ANALYSIS_BACKLOG_BATCH_SIZE", _BATCH)
    monkeypatch.setattr(
        settings, "ANALYSIS_SCORING_LAG_TOLERANCE_SECONDS", int(_TOLERANCE.total_seconds())
    )
    monkeypatch.setattr(settings, "ANALYSIS_ENGINE_INTERVAL_SECONDS", _INTERVAL_SECONDS)
    # The walk alone; TestRecentPass turns the recent region on.
    monkeypatch.setattr(settings, "ANALYSIS_BACKLOG_RECENT_SECONDS", 0)
    monkeypatch.setattr(analysis, "datetime", _Frozen)
    monkeypatch.setattr(analysis, "_scoring", analysis._ScoringState())
    monkeypatch.setattr(analysis, "_logged_state", None)


def _backlog(monkeypatch, fake: _FakeBacklog) -> _FakeBacklog:
    monkeypatch.setattr(engine, "pick_backlog_async", fake.pick)
    monkeypatch.setattr(engine, "run_backlog_once_async", fake.score)
    return fake


def _start_at(saved, checkpoint: datetime) -> None:
    saved[NETWORK] = checkpoint


class TestCheckpointStart:
    async def test_a_saved_checkpoint_is_resumed(self, monkeypatch, saved):
        _start_at(saved, _SETTLED - timedelta(hours=2))
        monkeypatch.setattr(
            analysis.clickhouse, "earliest_ingestion", lambda n: pytest.fail("must not scan")
        )
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert fake.picks[0][0] == _SETTLED - timedelta(hours=2)

    async def test_without_one_the_first_pass_checks_everything_ever_ingested(
        self, monkeypatch, saved
    ):
        first = NOW - timedelta(days=3)
        monkeypatch.setattr(analysis.clickhouse, "earliest_ingestion", lambda n: first)
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert fake.picks[0][0] == first
        assert saved[NETWORK] == _SETTLED

    async def test_nothing_ingested_yet_leaves_no_checkpoint(self, monkeypatch, saved):
        monkeypatch.setattr(analysis.clickhouse, "earliest_ingestion", lambda n: None)
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert fake.picks == []
        assert NETWORK not in saved


class TestCheckpointAdvance:
    async def test_a_clean_history_brings_it_to_the_settle_margin(self, monkeypatch, saved):
        _start_at(saved, NOW - timedelta(days=10))
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert saved[NETWORK] == _SETTLED
        assert fake.batches == []
        assert analysis._scoring.oldest_unscored is None

    async def test_it_never_passes_an_unscored_transaction(self, monkeypatch, saved):
        _start_at(saved, NOW - timedelta(days=10))
        waiting = NOW - timedelta(days=5, hours=3)
        _backlog(monkeypatch, _FakeBacklog([waiting], held=[waiting]))
        for _ in range(3):
            await analysis._backlog_tick(NETWORK)
            assert saved[NETWORK] <= waiting
        assert analysis._scoring.oldest_unscored == waiting

    async def test_it_moves_on_once_the_retry_resolves(self, monkeypatch, saved):
        _start_at(saved, NOW - timedelta(days=10))
        waiting = NOW - timedelta(days=5, hours=3)
        fake = _backlog(monkeypatch, _FakeBacklog([waiting], held=[waiting]))
        await analysis._backlog_tick(NETWORK)
        fake.held.clear()
        await analysis._backlog_tick(NETWORK)
        assert fake.stamps() == []
        assert saved[NETWORK] == _SETTLED
        assert analysis._scoring.oldest_unscored is None

    async def test_a_slice_drains_oldest_first_a_batch_at_a_time(self, monkeypatch, saved):
        _start_at(saved, _SETTLED - timedelta(days=2))
        stamps = [_SETTLED - timedelta(days=1, minutes=i) for i in range(1, _PICK + _BATCH)]
        fake = _backlog(monkeypatch, _FakeBacklog(stamps))
        stamp_of = dict(fake.unscored)
        await analysis._backlog_tick(NETWORK)
        assert fake.stamps() == []
        assert saved[NETWORK] == _SETTLED
        # Picked up to the pick size, then scored in batches, oldest first.
        assert max(len(b) for b in fake.batches) == _BATCH
        assert [stamp_of[h] for h in fake.batches[0]] == sorted(stamps)[:_BATCH]
        first_round = sum(len(b) for b in fake.batches[: -(-_PICK // _BATCH)])
        assert first_round == _PICK

    async def test_the_settle_margin_is_still_scored_but_not_checkpointed(self, monkeypatch, saved):
        # A transaction that just left the live window is the lane's work at
        # once, but the checkpoint stays clear of rows that may still land.
        _start_at(saved, _SETTLED)
        just_out = NOW - _WINDOW - timedelta(minutes=1)
        fake = _backlog(monkeypatch, _FakeBacklog([just_out]))
        await analysis._backlog_tick(NETWORK)
        assert fake.stamps() == []
        assert saved[NETWORK] == _SETTLED

    async def test_a_tick_ends_although_the_window_keeps_moving(self, monkeypatch, saved):
        # Against a clock that advances on every reading, chasing the live
        # window's edge would pick an ever thinner slice there, forever.
        ticking = iter(NOW + timedelta(seconds=s) for s in range(10**6))

        class _Ticking(datetime):
            @classmethod
            def now(cls, tz=None):
                return next(ticking)

        monkeypatch.setattr(analysis, "datetime", _Ticking)
        _start_at(saved, NOW - timedelta(days=2))
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert len(fake.picks) <= 3

    async def test_the_lane_never_reaches_into_the_live_window(self, monkeypatch, saved):
        _start_at(saved, NOW - timedelta(days=2))
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert max(until for _, until in fake.picks) == NOW - _WINDOW

    async def test_nothing_moves_while_scoring_is_off(self, monkeypatch, saved):
        # Picks would come back empty because nothing scores: passing them
        # would leave the pause's transactions behind the checkpoint forever.
        monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
        _start_at(saved, NOW - timedelta(days=10))
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert fake.picks == []
        assert saved[NETWORK] == NOW - timedelta(days=10)

    async def test_a_score_the_pick_cannot_see_stops_the_tick(self, monkeypatch, saved, caplog):
        # Reported scored yet picked again: re-scoring it forever would be a
        # silent hot loop, so the tick stops and says so.
        _start_at(saved, NOW - timedelta(days=2))
        stamp = NOW - timedelta(days=1)
        fake = _backlog(monkeypatch, _FakeBacklog([stamp]))

        async def _scored_but_still_unscored(network, hashes, since, until, should_stop=None):
            fake.batches.append(list(hashes))
            return BacklogBatch(len(hashes), len(hashes))

        monkeypatch.setattr(engine, "run_backlog_once_async", _scored_but_still_unscored)
        with caplog.at_level(logging.ERROR, logger=analysis.logger.name):
            await analysis._backlog_tick(NETWORK)
        assert len(fake.batches) == 1
        assert saved[NETWORK] <= stamp
        assert "still picked as unscored" in caplog.text


class TestClockStep:
    """A checkpoint is only ever saved at or behind its time's settle margin."""

    async def test_a_checkpoint_ahead_of_the_clock_is_rewound(self, monkeypatch, saved, caplog):
        # The host clock stepped back a day: left ahead, the checkpoint would
        # sit in front of every transaction the stepped clock stamps meanwhile.
        _start_at(saved, _SETTLED + timedelta(days=1))
        stepped_back = NOW - timedelta(hours=1)
        fake = _backlog(monkeypatch, _FakeBacklog([stepped_back]))
        with caplog.at_level(logging.WARNING, logger=analysis.logger.name):
            await analysis._backlog_tick(NETWORK)
        assert saved[NETWORK] == _SETTLED
        assert analysis._scoring.checkpoint == _SETTLED
        assert fake.stamps() == []
        assert "ahead of its settle margin" in caplog.text

    async def test_a_caught_up_checkpoint_is_left_alone(self, monkeypatch, saved, caplog):
        _start_at(saved, _SETTLED)
        _backlog(monkeypatch, _FakeBacklog())
        with caplog.at_level(logging.WARNING, logger=analysis.logger.name):
            await analysis._backlog_tick(NETWORK)
        assert saved[NETWORK] == _SETTLED
        assert "ahead of its settle margin" not in caplog.text

    async def test_a_new_install_starts_at_the_margin_without_a_warning(
        self, monkeypatch, saved, caplog
    ):
        # Its first ingestion is minutes old, past the margin: legitimately so.
        monkeypatch.setattr(
            analysis.clickhouse, "earliest_ingestion", lambda n: NOW - timedelta(minutes=5)
        )
        _backlog(monkeypatch, _FakeBacklog())
        with caplog.at_level(logging.WARNING, logger=analysis.logger.name):
            await analysis._backlog_tick(NETWORK)
        assert saved[NETWORK] == _SETTLED
        assert "ahead of its settle margin" not in caplog.text


class TestSliceTail:
    async def test_a_caught_up_tail_is_picked_with_its_slice(self, monkeypatch, saved):
        # Caught up, the tail is the interval plus the last tick's run: on its
        # own it would cost a whole pick and could not move the checkpoint.
        monkeypatch.setattr(
            settings, "ANALYSIS_BACKLOG_SLICE_SECONDS", int(_SETTLE.total_seconds())
        )
        last_tick_ran = timedelta(minutes=2)
        start = _SETTLED - timedelta(seconds=_INTERVAL_SECONDS) - last_tick_ran
        _start_at(saved, start)
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert fake.picks == [(start, NOW - _WINDOW)]

    async def test_a_longer_tail_is_a_slice_of_its_own(self, monkeypatch, saved):
        monkeypatch.setattr(
            settings, "ANALYSIS_BACKLOG_SLICE_SECONDS", int(_SETTLE.total_seconds())
        )
        start = _SETTLED - _SETTLE / 2 - timedelta(seconds=1)
        _start_at(saved, start)
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert len(fake.picks) == 2


class TestRecentPass:
    """What just left the live window is scored ahead of the oldest-first walk.

    Without it a transaction pushed out of the live window waits behind every
    older unscored one, which on a large backlog is months.
    """

    @pytest.fixture(autouse=True)
    def _region(self, monkeypatch):
        monkeypatch.setattr(
            settings, "ANALYSIS_BACKLOG_RECENT_SECONDS", int(_REGION.total_seconds())
        )

    def _history(self, count, hours_back=_HISTORY_HOURS_BACK):
        # Far behind the region, in one slice of the walk.
        return [NOW - timedelta(hours=hours_back, minutes=i) for i in range(count)]

    async def test_an_aged_out_transaction_is_scored_before_older_history(self, monkeypatch, saved):
        _start_at(saved, NOW - timedelta(hours=_HISTORY_HOURS_BACK + 1))
        aged_out = NOW - _WINDOW - timedelta(minutes=5)
        # A burst sent after it, to push it out of the live window: oldest
        # first within the region, so it queues behind the transaction.
        burst = [NOW - _WINDOW - timedelta(minutes=1)] * _BATCH
        stamps = self._history(_PICK) + [aged_out, *burst]
        fake = _backlog(monkeypatch, _FakeBacklog(stamps))
        target = f"tx{_PICK:06d}"
        await analysis._backlog_tick(NETWORK)
        assert fake.batches[0][0] == target
        assert not fake.unscored, "the walk still reaches everything"

    async def test_it_takes_its_turn_between_batches_of_the_walk(self, monkeypatch, saved):
        _start_at(saved, NOW - timedelta(hours=_HISTORY_HOURS_BACK + 1))
        fake = _backlog(monkeypatch, _FakeBacklog(self._history(_PICK)))
        clock = iter(range(0, 10**6, _INTERVAL_SECONDS))
        monkeypatch.setattr(analysis.time, "monotonic", lambda: next(clock))
        walk_score = fake.score
        arrived = NOW - _WINDOW - timedelta(minutes=1)

        async def score(network, hashes, since, until, should_stop=None):
            if len(fake.batches) == 1:
                # Ages out of the live window while the walk drains.
                fake.unscored["txlate"] = arrived
            return await walk_score(network, hashes, since, until)

        monkeypatch.setattr(engine, "run_backlog_once_async", score)
        await analysis._backlog_tick(NETWORK)
        late_batch = next(i for i, b in enumerate(fake.batches) if "txlate" in b)
        walk_batches = -(-_PICK // _BATCH)  # ceil: the walk's batches for one pick
        assert late_batch < walk_batches, "scored before the walk finished its pick"

    async def test_it_never_moves_the_checkpoint(self, monkeypatch, saved):
        held = NOW - timedelta(hours=_HISTORY_HOURS_BACK)
        _start_at(saved, held - timedelta(hours=1))
        recent = NOW - _WINDOW - timedelta(minutes=5)
        fake = _backlog(monkeypatch, _FakeBacklog([held, recent], held=[held]))
        await analysis._backlog_tick(NETWORK)
        assert list(fake.unscored.values()) == [held], "the recent one is scored"
        assert saved[NETWORK] <= held, "and the checkpoint waits on the held one"

    async def test_caught_up_it_costs_no_pick(self, monkeypatch, saved):
        # The walk is inside the region already: it scores the region itself.
        _start_at(saved, _SETTLED)
        fake = _backlog(monkeypatch, _FakeBacklog())
        await analysis._backlog_tick(NETWORK)
        assert all(since >= _SETTLED for since, _ in fake.picks)
        assert len(fake.picks) == 1

    async def test_a_deferred_transaction_picked_again_does_not_stop_the_pass(
        self, monkeypatch, saved, caplog
    ):
        """Mainnet, 2026-11-02 21:33: a recent transaction whose sandwich
        enrichment failed was deferred, picked again on the next turn, read as
        a loop, and the pass left the region for the rest of a tick that lasts
        the whole drain. A transaction that ages out after it must still be
        scored ahead of the walk."""
        _start_at(saved, NOW - timedelta(hours=_HISTORY_HOURS_BACK + 1))
        deferred = NOW - _WINDOW - timedelta(minutes=30)
        fake = _backlog(
            monkeypatch,
            _FakeBacklog(self._history(_PICK) + [deferred], held=[deferred]),
        )
        clock = iter(range(0, 10**6, _INTERVAL_SECONDS))
        monkeypatch.setattr(analysis.time, "monotonic", lambda: next(clock))
        walk_score = fake.score
        arrived = NOW - _WINDOW - timedelta(minutes=1)

        async def score(network, hashes, since, until, should_stop=None):
            if len(fake.batches) == 2:
                # Ages out of the live window after the deferral was picked again.
                fake.unscored["txlate"] = arrived
            return await walk_score(network, hashes, since, until)

        monkeypatch.setattr(engine, "run_backlog_once_async", score)
        with caplog.at_level(logging.ERROR, logger=analysis.logger.name):
            await analysis._backlog_tick(NETWORK)
        deferred_hash = f"tx{_PICK:06d}"
        assert sum(deferred_hash in b for b in fake.batches) > 2, "retried on every turn"
        late_batch = next(i for i, b in enumerate(fake.batches) if "txlate" in b)
        # _history counts back in time, so its first stamp is the newest: the
        # walk, oldest first, scores it last.
        last_of_history = f"tx{0:06d}"
        walk_done = next(i for i, b in enumerate(fake.batches) if last_of_history in b)
        assert late_batch < walk_done, "scored before the walk finished its pick"
        assert "still picked as unscored" not in caplog.text

    async def test_a_scored_transaction_picked_again_is_left_to_the_walk(
        self, monkeypatch, saved, caplog
    ):
        _start_at(saved, NOW - timedelta(hours=_HISTORY_HOURS_BACK + 1))
        recent = NOW - _WINDOW - timedelta(minutes=5)
        fake = _backlog(monkeypatch, _FakeBacklog(self._history(_PICK) + [recent]))
        clock = iter(range(0, 10**6, _INTERVAL_SECONDS))
        monkeypatch.setattr(analysis.time, "monotonic", lambda: next(clock))

        async def scored_but_still_unscored(network, hashes, since, until, should_stop=None):
            fake.batches.append(list(hashes))
            return BacklogBatch(len(hashes), len(hashes))

        monkeypatch.setattr(engine, "run_backlog_once_async", scored_but_still_unscored)
        with caplog.at_level(logging.ERROR, logger=analysis.logger.name):
            await analysis._backlog_tick(NETWORK)
        recent_hash = f"tx{_PICK:06d}"
        assert sum(recent_hash in b for b in fake.batches) == 1
        assert "recent region" in caplog.text


class TestScoringStatus:
    def _state(self, **scoring):
        for name, value in scoring.items():
            setattr(analysis._scoring, name, value)
        return analysis.scoring_status()["state"]

    def test_starting_until_the_live_lane_finishes_a_poll(self):
        assert self._state(live_started_at=NOW, checkpoint=_SETTLED) == "starting"

    def test_stalled_when_the_first_poll_never_finishes(self):
        # Every tick failing from boot is a stall, not a slow start.
        started = NOW - timedelta(seconds=_INTERVAL_SECONDS) - _TOLERANCE - timedelta(seconds=1)
        assert self._state(live_started_at=started, checkpoint=_SETTLED) == "stalled"

    def test_starting_while_the_spend_edges_history_fills(self):
        # Neither lane scores meanwhile; filling outlasting the stall tolerance
        # is not a stall while chunks keep landing.
        started = NOW - timedelta(seconds=_INTERVAL_SECONDS) - _TOLERANCE - timedelta(seconds=1)
        state = self._state(
            edges_filling=True,
            live_started_at=started,
            edges_progress_at=NOW,
            checkpoint=_SETTLED,
        )
        assert state == "starting"

    def test_stalled_when_the_spend_edges_fill_stops_advancing(self):
        # A chunk failing on every retry holds both lanes for good: that is a
        # scoring outage, not a slow start.
        stuck = NOW - timedelta(seconds=_INTERVAL_SECONDS) - _TOLERANCE - timedelta(seconds=1)
        state = self._state(
            edges_filling=True,
            live_started_at=stuck,
            edges_progress_at=stuck,
            checkpoint=_SETTLED,
        )
        assert state == "stalled"

    def test_stalled_when_the_spend_edges_fill_never_finishes_a_chunk(self):
        started = NOW - timedelta(seconds=_INTERVAL_SECONDS) - _TOLERANCE - timedelta(seconds=1)
        state = self._state(edges_filling=True, live_started_at=started, checkpoint=_SETTLED)
        assert state == "stalled"

    def test_disabled_while_analysis_is_off(self, monkeypatch):
        # The live lane's ticks still finish, scoring nothing.
        monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
        assert self._state(live_polled_at=NOW, checkpoint=_SETTLED) == "disabled"

    def test_ok_when_caught_up(self):
        assert self._state(live_polled_at=NOW, checkpoint=_SETTLED) == "ok"

    def test_stalled_when_the_live_lane_stops_finishing_ticks(self):
        stale = NOW - timedelta(seconds=_INTERVAL_SECONDS) - _TOLERANCE - timedelta(seconds=1)
        assert self._state(live_polled_at=stale, checkpoint=_SETTLED) == "stalled"

    def test_behind_when_a_transaction_waits_past_the_window(self):
        waited = NOW - _WINDOW - _TOLERANCE - timedelta(seconds=1)
        state = self._state(live_polled_at=NOW, checkpoint=_SETTLED, oldest_unscored=waited)
        assert state == "behind"

    def test_behind_while_the_checkpoint_has_not_checked_the_history(self):
        # Weeks unscored with ingestion at the tip: nothing else would tell.
        state = self._state(live_polled_at=NOW, checkpoint=NOW - timedelta(days=25))
        assert state == "behind"

    def test_with_the_backlog_lane_off_it_is_never_ok(self, monkeypatch):
        # Nothing would then score, or know of, what the live lane misses.
        monkeypatch.setattr(settings, "ANALYSIS_BACKLOG_ENABLED", False)
        assert self._state(live_polled_at=NOW) == "unguarded"

    def test_an_unguarded_state_is_logged_as_a_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "ANALYSIS_BACKLOG_ENABLED", False)
        self._state(live_polled_at=NOW)
        with caplog.at_level(logging.INFO, logger=analysis.logger.name):
            analysis._log_scoring_state()
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    def test_the_report_carries_the_lanes_timestamps(self):
        self._state(live_polled_at=NOW, checkpoint=_SETTLED)
        status = analysis.scoring_status()
        assert status["checkpoint"] == "2026-11-02T10:50:00Z"
        assert status["live_polled_at"] == "2026-11-02T12:00:00Z"
        assert status["oldest_unscored_at"] is None

    def test_a_change_is_logged_once(self, caplog):
        self._state(live_polled_at=NOW, checkpoint=NOW - timedelta(days=25))
        with caplog.at_level(logging.INFO, logger=analysis.logger.name):
            analysis._log_scoring_state()
            analysis._log_scoring_state()
            self._state(checkpoint=_SETTLED)
            analysis._log_scoring_state()
        messages = [r.getMessage() for r in caplog.records]
        assert sum("Scoring is behind" in m for m in messages) == 1
        assert sum("Scoring recovered" in m for m in messages) == 1
        assert caplog.records[0].levelno == logging.ERROR


class TestRetentionAtRisk:
    """Rows behind the checkpoint that expire unscored are recorded nowhere,
    so the report must say so before they expire."""

    @pytest.fixture(autouse=True)
    def _retention(self, monkeypatch):
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_TRANSACTIONS", 0)
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_IO", _RETENTION_DAYS)
        analysis._scoring.live_polled_at = NOW

    def _report(self, checkpoint):
        analysis._scoring.checkpoint = checkpoint
        return analysis.scoring_status()

    def _past_share(self, days=_RETENTION_DAYS):
        limit = timedelta(days=days) * settings.ANALYSIS_RETENTION_WARNING_SHARE
        return NOW - limit - timedelta(hours=1)

    def test_a_checkpoint_past_its_share_of_the_retention_is_at_risk(self):
        report = self._report(self._past_share())
        assert report["retention_at_risk"] is True
        assert report["state"] == "behind"

    def test_caught_up_is_not_at_risk_however_short_the_retention(self, monkeypatch):
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_IO", 1)
        report = self._report(_SETTLED)
        assert report["retention_at_risk"] is False
        assert report["state"] == "ok"

    def test_the_shortest_retention_counts(self, monkeypatch):
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_TRANSACTIONS", _RETENTION_DAYS * 10)
        assert self._report(self._past_share())["retention_at_risk"] is True

    def test_no_retention_no_risk(self, monkeypatch):
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_IO", 0)
        assert self._report(NOW - timedelta(days=365))["retention_at_risk"] is False

    def test_the_risk_is_logged_even_while_already_behind(self, caplog):
        # Behind (history not yet checked), but well inside the retention.
        self._report(NOW - timedelta(hours=3))
        with caplog.at_level(logging.ERROR, logger=analysis.logger.name):
            analysis._log_scoring_state()
            self._report(self._past_share())
            analysis._log_scoring_state()
        assert any("retention at risk" in r.getMessage() for r in caplog.records)


class _TickSlept(Exception):
    """Raised from the sleep after the first tick to end _loop."""


class TestLiveDrainPacing:
    """What the live lane leaves when its drain hits the cap ages into the
    backlog lane, where a late score costs far more: the next tick must not
    wait a whole interval."""

    async def _tick_sleep(self, monkeypatch, processed=0, run_once=None):
        monkeypatch.setattr(settings, "BASELINE_BOOTSTRAP_ON_STARTUP", False)
        monkeypatch.setattr(settings, "SCORER_FAKE_TOKEN_ENABLED", False)
        tick_done = []
        # The last duty of a tick: the sleep after it is the one between ticks.
        monkeypatch.setattr(
            analysis, "_maybe_start_baseline_recompute", lambda: tick_done.append(True)
        )
        monkeypatch.setattr(analysis, "_initial_baseline_schedule", AsyncMock(return_value=0.0))
        monkeypatch.setattr(
            analysis.engine, "run_once_async", run_once or AsyncMock(return_value=processed)
        )
        slept = []

        async def sleep(seconds):
            slept.append(seconds)
            if tick_done:
                raise _TickSlept

        monkeypatch.setattr(analysis.asyncio, "sleep", sleep)
        with pytest.raises(_TickSlept):
            await analysis._loop()
        return slept[-1]

    async def test_a_saturated_drain_starts_the_next_tick_at_once(self, monkeypatch):
        full = settings.ANALYSIS_ENGINE_BATCH_SIZE
        assert await self._tick_sleep(monkeypatch, full) == (
            settings.ANALYSIS_ENGINE_DRAIN_SLEEP_SECONDS
        )

    async def test_a_drained_window_waits_the_interval(self, monkeypatch):
        assert await self._tick_sleep(monkeypatch, 0) == settings.ANALYSIS_ENGINE_INTERVAL_SECONDS

    async def test_every_finished_poll_counts_as_progress(self, monkeypatch):
        # Stamped once per tick, a saturated tick of slow batches outlasts the
        # stall tolerance while every poll succeeds.
        full = settings.ANALYSIS_ENGINE_BATCH_SIZE
        polls = AsyncMock(side_effect=[full, RuntimeError("ClickHouse down")])
        await self._tick_sleep(monkeypatch, run_once=polls)
        assert analysis._scoring.live_polled_at == NOW


class TestSpendEdgesGate:
    """No lane scores before the spend_edges history is whole: a cycle scored
    against a partial graph misses the legs not yet filled in, for good."""

    async def test_both_lanes_wait_for_the_backfill(self, monkeypatch):
        import asyncio

        filled = asyncio.Event()
        calls: list[dict] = []

        async def backfill(network, should_stop, history_before=None, on_progress=None):
            calls.append({"history_before": history_before})
            on_progress(1)
            await filled.wait()

        live = AsyncMock(return_value=0)
        tick = AsyncMock()
        monkeypatch.setattr(analysis.spend_edges, "ensure_backfilled", backfill)
        monkeypatch.setattr(analysis.engine, "run_once_async", live)
        monkeypatch.setattr(analysis, "_backlog_tick", tick)
        monkeypatch.setattr(analysis, "_initial_baseline_schedule", AsyncMock(return_value=0.0))
        monkeypatch.setattr(settings, "BASELINE_BOOTSTRAP_ON_STARTUP", False)
        monkeypatch.setattr(settings, "SCORER_FAKE_TOKEN_ENABLED", False)
        monkeypatch.setattr(analysis, "_maybe_start_baseline_recompute", lambda: None)
        monkeypatch.setattr(analysis, "_task", None)
        analysis.start()
        try:
            for _ in range(_GATE_YIELDS):
                await asyncio.sleep(0)
            assert not live.called and not tick.called
            status = analysis.scoring_status()
            assert status["state"] == "starting"
            assert status["spend_edges_filled_chunks"] == 1
            # The loss check reaches back only to before this start's ingestion.
            assert calls[0]["history_before"] is not None
            filled.set()
            for _ in range(_GATE_YIELDS):
                await asyncio.sleep(0)
            assert live.called and tick.called
            assert analysis.scoring_status()["spend_edges_filling"] is False
        finally:
            analysis.stop()
