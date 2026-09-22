"""When and where the baseline recompute runs.

The recompute is the heaviest scan in the app (~1,850 quantileExact scans on
mainnet). It used to be awaited inside the scoring loop, which stopped all
scoring for its whole run, daily and on every restart; after the 2026-09-22
mainnet restart there were zero hop queries for ~17 minutes. These pin the
replacement: it runs beside the loop on its own worker, one at a time, backs
off after a failure, resumes its schedule across restarts, and ends at the next
scope when the task stops.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from app.analysis import baselines
from app.config import settings
from app.db import clickhouse
from app.tasks import analysis as task

# Upper bound on any single wait here, so a regression fails in seconds instead
# of hanging the suite.
_WAIT_SECONDS = 5


@pytest.fixture(autouse=True)
def fresh_schedule(monkeypatch):
    monkeypatch.setattr(task, "_last_baseline_recompute", 0.0)
    monkeypatch.setattr(task, "_baseline_retry_not_before", 0.0)
    monkeypatch.setattr(task, "_baseline_task", None)
    monkeypatch.setattr(task, "_baseline_stop", None)


class _HeldRecompute:
    """Stands in for recompute_all_baselines and holds until released.

    The maintenance executor has one worker, so every test that starts one of
    these must release it (see _released) or later tests queue behind it.
    """

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.thread_name = ""
        self.should_stop = None
        self.calls = 0

    def __call__(self, network, max_scripts, should_stop=None):
        self.calls += 1
        self.thread_name = threading.current_thread().name
        self.should_stop = should_stop
        self.started.set()
        self.release.wait(_WAIT_SECONDS)
        return 1

    async def wait_started(self):
        assert await asyncio.to_thread(self.started.wait, _WAIT_SECONDS), "never started"


@pytest.fixture
def held(monkeypatch):
    fake = _HeldRecompute()
    monkeypatch.setattr(baselines, "recompute_all_baselines", fake)
    yield fake
    fake.release.set()  # always free the single maintenance worker


class TestRunsBesideTheScoringLoop:
    async def test_a_due_recompute_does_not_hold_up_the_loop(self, held):
        task._maybe_start_baseline_recompute()  # synchronous: nothing is awaited
        await held.wait_started()
        assert not task._baseline_task.done(), "the loop is free while the scan runs"
        held.release.set()
        await asyncio.wait_for(task._baseline_task, _WAIT_SECONDS)
        assert task._last_baseline_recompute > 0

    async def test_it_runs_on_the_maintenance_worker(self, held):
        """Not on the three role workers: an hour-long scan there would starve
        ingestion, API reads or scoring."""
        task._maybe_start_baseline_recompute()
        await held.wait_started()
        assert held.thread_name.startswith("clickhouse-maintenance"), held.thread_name

    async def test_a_second_recompute_never_starts_while_one_runs(self, held):
        task._maybe_start_baseline_recompute()
        first = task._baseline_task
        await held.wait_started()
        task._maybe_start_baseline_recompute()  # the next engine tick
        assert task._baseline_task is first
        held.release.set()
        await asyncio.wait_for(first, _WAIT_SECONDS)
        assert held.calls == 1


class TestSchedule:
    async def test_a_completed_recompute_is_not_due_again_within_the_interval(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            baselines, "recompute_all_baselines", lambda *a, **k: calls.append(a) or 1
        )
        task._maybe_start_baseline_recompute()
        await asyncio.wait_for(task._baseline_task, _WAIT_SECONDS)
        finished = task._baseline_task
        task._maybe_start_baseline_recompute()
        assert task._baseline_task is finished and len(calls) == 1

    async def test_a_failed_recompute_waits_out_its_backoff(self, monkeypatch):
        """Retrying on every tick would re-run the heaviest scan in the app back
        to back against a server that just failed it."""

        def fails(*args, **kwargs):
            raise RuntimeError("clickhouse unavailable")

        monkeypatch.setattr(baselines, "recompute_all_baselines", fails)
        before = time.time()
        task._maybe_start_baseline_recompute()
        await asyncio.wait_for(task._baseline_task, _WAIT_SECONDS)
        assert task._baseline_retry_not_before >= before + settings.BASELINE_RECOMPUTE_RETRY_SECONDS
        assert task._last_baseline_recompute == 0.0, "a failure is not a completed run"
        failed = task._baseline_task
        task._maybe_start_baseline_recompute()  # the next engine tick
        assert task._baseline_task is failed, "retried inside the backoff"

    async def test_a_restart_resumes_from_the_last_recompute_on_record(self, monkeypatch):
        landed = time.time() - task._SECONDS_PER_HOUR
        monkeypatch.setattr(settings, "BASELINE_RECOMPUTE_ON_STARTUP", False)
        monkeypatch.setattr(clickhouse, "latest_full_baseline_recompute", lambda network: landed)
        assert await task._initial_baseline_schedule() == landed
        monkeypatch.setattr(task, "_last_baseline_recompute", landed)
        task._maybe_start_baseline_recompute()
        assert task._baseline_task is None, "a restart re-ran a recompute from an hour ago"

    async def test_a_fresh_deployment_recomputes_at_once(self, monkeypatch):
        monkeypatch.setattr(settings, "BASELINE_RECOMPUTE_ON_STARTUP", False)
        monkeypatch.setattr(clickhouse, "latest_full_baseline_recompute", lambda network: 0.0)
        assert await task._initial_baseline_schedule() == 0.0

    async def test_an_unreadable_schedule_recomputes_at_once(self, monkeypatch):
        """A missing reading must never postpone a recompute."""

        def unreadable(network):
            raise RuntimeError("baselines table unreachable")

        monkeypatch.setattr(settings, "BASELINE_RECOMPUTE_ON_STARTUP", False)
        monkeypatch.setattr(clickhouse, "latest_full_baseline_recompute", unreadable)
        assert await task._initial_baseline_schedule() == 0.0

    async def test_the_startup_setting_forces_a_recompute(self, monkeypatch):
        """For a deploy that changes how baselines are computed."""
        monkeypatch.setattr(settings, "BASELINE_RECOMPUTE_ON_STARTUP", True)
        monkeypatch.setattr(
            clickhouse, "latest_full_baseline_recompute", lambda network: time.time()
        )
        assert await task._initial_baseline_schedule() == 0.0


class _FirstTick(Exception):
    """Raised from the first tick to end _loop, which otherwise runs forever."""


class TestLoopWiring:
    async def test_the_loop_seeds_the_schedule_before_its_first_tick(self, monkeypatch):
        """_loop must resume the schedule before it first checks for a due run;
        without that, every restart recomputes regardless of the store."""
        landed = time.time() - task._SECONDS_PER_HOUR
        monkeypatch.setattr(settings, "BASELINE_BOOTSTRAP_ON_STARTUP", False)
        monkeypatch.setattr(settings, "SCORER_FAKE_TOKEN_ENABLED", False)

        async def no_backlog(network):
            return 0

        async def schedule():
            return landed

        seen = []

        def first_tick():
            seen.append(task._last_baseline_recompute)
            raise _FirstTick

        monkeypatch.setattr(task.engine, "run_once_async", no_backlog)
        monkeypatch.setattr(task, "_initial_baseline_schedule", schedule)
        monkeypatch.setattr(task, "_maybe_start_baseline_recompute", first_tick)

        with pytest.raises(_FirstTick):
            await task._loop()
        assert seen == [landed]


class TestStop:
    async def test_stop_signals_the_recompute_in_flight(self, held):
        task._maybe_start_baseline_recompute()
        await held.wait_started()
        task.stop()
        assert held.should_stop(), "the scan would run on to the end of its hour"

    def test_the_recompute_returns_between_scopes_once_stopped(self, monkeypatch):
        finished = []
        multiple_sat = []
        monkeypatch.setattr(baselines, "compute_global_baselines", lambda network: [])
        monkeypatch.setattr(
            baselines, "get_active_script_addresses", lambda network, limit: ["s1", "s2", "s3"]
        )
        monkeypatch.setattr(
            baselines,
            "compute_script_baselines",
            lambda network, addr: finished.append(addr) or [addr],
        )
        monkeypatch.setattr(
            baselines,
            "compute_multiple_sat_per_script_baselines",
            lambda network: multiple_sat.append(network) or [],
        )

        total = baselines.recompute_all_baselines("preprod", 3, should_stop=lambda: bool(finished))

        assert finished == ["s1"], "a stop must end the run at the next scope"
        assert multiple_sat == [], "nor run the multiple_sat step after it"
        assert total == 1, "the scope it finished still counts"


class TestEveryRecomputeScanIsCapped:
    """The percentile scans are covered in test_baselines_percentile_sql; these
    are the recompute's other two scans."""

    _CAP = property(lambda self: {"max_threads": settings.BASELINE_QUERY_MAX_THREADS})

    def test_the_active_script_lookup_is_capped(self, monkeypatch):
        client = MagicMock()
        client.execute.return_value = []
        monkeypatch.setattr(clickhouse, "_get_client", lambda: client)
        baselines.get_active_script_addresses("preprod")
        assert client.execute.call_args.kwargs["settings"] == self._CAP

    def test_the_multiple_sat_scan_is_capped(self, monkeypatch):
        seen = {}

        def query(network, window_days, min_samples, query_settings=None):
            seen["settings"] = query_settings
            return []

        monkeypatch.setattr(clickhouse, "query_multiple_sat_extraction_percentiles", query)
        baselines.compute_multiple_sat_per_script_baselines("preprod")
        assert seen["settings"] == self._CAP

    def test_the_multiple_sat_query_hands_its_settings_to_the_driver(self, monkeypatch):
        client = MagicMock()
        client.execute.return_value = []
        monkeypatch.setattr(clickhouse, "_get_client", lambda: client)
        clickhouse.query_multiple_sat_extraction_percentiles(
            "preprod",
            baselines._PER_SCRIPT_WINDOW_DAYS,
            settings.BASELINE_MIN_SAMPLES,
            query_settings=self._CAP,
        )
        assert client.execute.call_args.kwargs["settings"] == self._CAP
