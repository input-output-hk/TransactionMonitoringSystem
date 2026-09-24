"""app.tasks.housekeeping runs independently of ANALYSIS_ENGINE_ENABLED: the
stale-PENDING DROPPED sweep, retention, and auth purge used to live inside
the analysis engine's loop, so disabling scoring silently disabled all of
them too (review finding). These tests drive one _tick() directly.
"""

import pytest

from app.analysis.graph import AddressIndexCheck
from app.config import _ADDRESS_INDEX_CHECK_MIN_INTERVAL_SECONDS, settings
from app.tasks import housekeeping

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    housekeeping._last_retention_sweep = 0.0
    housekeeping._last_address_index_check = 0.0
    housekeeping._address_index_status = {"state": "unchecked"}
    # Off unless a test turns it on: a hermetic tick must not reach ClickHouse.
    monkeypatch.setattr(settings, "ADDRESS_INDEX_CHECK_INTERVAL_SECONDS", 0)
    yield
    housekeeping._last_retention_sweep = 0.0
    housekeeping._last_address_index_check = 0.0
    housekeeping._address_index_status = {"state": "unchecked"}


async def test_tick_runs_stale_pending_sweep_every_call(monkeypatch):
    calls = []

    async def fake_mark(network, ttl):
        calls.append((network, ttl))
        return 3

    monkeypatch.setattr(housekeeping.postgres, "mark_dropped_pending_txs", fake_mark)
    # Keep the throttled retention block from firing so this test isolates
    # just the every-tick sweep.
    monkeypatch.setattr(settings, "RETENTION_SWEEP_INTERVAL_HOURS", 999999)

    await housekeeping._tick()
    await housekeeping._tick()

    assert len(calls) == 2
    assert calls[0] == (settings.CARDANO_NETWORK, settings.LIFECYCLE_PENDING_TTL_SECONDS)


async def test_tick_survives_stale_pending_sweep_error(monkeypatch):
    async def boom(network, ttl):
        raise RuntimeError("db down")

    monkeypatch.setattr(housekeeping.postgres, "mark_dropped_pending_txs", boom)
    monkeypatch.setattr(settings, "RETENTION_SWEEP_INTERVAL_HOURS", 999999)

    await housekeeping._tick()  # must not raise


async def test_retention_sweep_runs_when_due_and_knobs_enabled(monkeypatch):
    monkeypatch.setattr(housekeeping.postgres, "mark_dropped_pending_txs", _ok(0))
    monkeypatch.setattr(settings, "RETENTION_SWEEP_INTERVAL_HOURS", 0)  # always due
    monkeypatch.setattr(settings, "LIFECYCLE_RETENTION_DAYS", 30)
    monkeypatch.setattr(settings, "MEMPOOL_COLLISION_RETENTION_DAYS", 0)
    monkeypatch.setattr(settings, "RAW_STORE_RETENTION_DAYS", 0)
    monkeypatch.setattr(settings, "AUDIT_LOG_RETENTION_DAYS", 0)
    monkeypatch.setattr(settings, "NOTIFY_DEDUP_RETENTION_DAYS", 0)

    pruned = {}

    async def fake_prune_lifecycle(network, days):
        pruned["lifecycle"] = (network, days)
        return 5

    monkeypatch.setattr(housekeeping.postgres, "prune_terminal_lifecycle", fake_prune_lifecycle)

    from app.auth import sessions as auth_sessions
    from app.auth import tokens as auth_tokens

    monkeypatch.setattr(auth_tokens, "purge_expired_tokens", _ok(0))
    monkeypatch.setattr(auth_sessions, "purge_expired_sessions", _ok(0))

    await housekeeping._tick()

    assert pruned["lifecycle"] == (settings.CARDANO_NETWORK, 30)


async def test_retention_sweep_is_throttled(monkeypatch):
    monkeypatch.setattr(housekeeping.postgres, "mark_dropped_pending_txs", _ok(0))
    monkeypatch.setattr(settings, "RETENTION_SWEEP_INTERVAL_HOURS", 24)
    monkeypatch.setattr(settings, "LIFECYCLE_RETENTION_DAYS", 30)

    calls = []

    async def fake_prune_lifecycle(network, days):
        calls.append(1)
        return 0

    monkeypatch.setattr(housekeeping.postgres, "prune_terminal_lifecycle", fake_prune_lifecycle)

    from app.auth import sessions as auth_sessions
    from app.auth import tokens as auth_tokens

    monkeypatch.setattr(auth_tokens, "purge_expired_tokens", _ok(0))
    monkeypatch.setattr(auth_sessions, "purge_expired_sessions", _ok(0))

    await housekeeping._tick()  # first call: due (last=0.0), runs
    await housekeeping._tick()  # second call: not due yet, skipped

    assert len(calls) == 1


async def test_retention_sweep_always_purges_auth_regardless_of_retention_knobs(monkeypatch):
    """Auth purge is unconditional (no retention days knob gates it)."""
    monkeypatch.setattr(housekeeping.postgres, "mark_dropped_pending_txs", _ok(0))
    monkeypatch.setattr(settings, "RETENTION_SWEEP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(settings, "LIFECYCLE_RETENTION_DAYS", 0)
    monkeypatch.setattr(settings, "MEMPOOL_COLLISION_RETENTION_DAYS", 0)
    monkeypatch.setattr(settings, "RAW_STORE_RETENTION_DAYS", 0)
    monkeypatch.setattr(settings, "AUDIT_LOG_RETENTION_DAYS", 0)
    monkeypatch.setattr(settings, "NOTIFY_DEDUP_RETENTION_DAYS", 0)

    from app.auth import sessions as auth_sessions
    from app.auth import tokens as auth_tokens

    purged = {}

    async def fake_purge_tokens():
        purged["tokens"] = True
        return 2

    async def fake_purge_sessions():
        purged["sessions"] = True
        return 1

    monkeypatch.setattr(auth_tokens, "purge_expired_tokens", fake_purge_tokens)
    monkeypatch.setattr(auth_sessions, "purge_expired_sessions", fake_purge_sessions)

    await housekeeping._tick()

    assert purged == {"tokens": True, "sessions": True}


def _ok(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn


class TestAddressIndexCheck:
    """The circular BFS sees a leg only through address_transactions, so a gap
    there must surface as a warning and in /health/detail, not stay silent."""

    @pytest.fixture(autouse=True)
    def _due(self, monkeypatch):
        monkeypatch.setattr(housekeeping.postgres, "mark_dropped_pending_txs", _ok(0))
        monkeypatch.setattr(settings, "RETENTION_SWEEP_INTERVAL_HOURS", 999999)
        monkeypatch.setattr(settings, "CYCLE_DETECTION_ENABLED", True)
        monkeypatch.setattr(
            settings,
            "ADDRESS_INDEX_CHECK_INTERVAL_SECONDS",
            _ADDRESS_INDEX_CHECK_MIN_INTERVAL_SECONDS,
        )

        async def inline(fn, *args):
            return fn(*args)

        monkeypatch.setattr(housekeeping.clickhouse, "_in_executor", inline)

    def _gaps(self, monkeypatch, result):
        from app.analysis import graph

        calls = []

        def fake(network, window):
            calls.append((network, window))
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(graph, "address_index_gaps", fake)
        return calls

    async def test_gaps_are_logged_and_reported(self, monkeypatch, caplog):
        calls = self._gaps(monkeypatch, AddressIndexCheck(missing=3, checked=40, unresolved=2))

        await housekeeping._tick()

        assert calls == [(settings.CARDANO_NETWORK, settings.ADDRESS_INDEX_CHECK_WINDOW_SECONDS)]
        status = housekeeping.address_index_status()
        assert status["state"] == "gaps"
        assert status["missing_spends"] == 3
        assert status["checked_spends"] == 40
        assert status["unresolved_inputs"] == 2
        assert any(
            r.levelname == "WARNING" and "address_transactions" in r.getMessage()
            for r in caplog.records
        )

    async def test_a_complete_index_reads_ok(self, monkeypatch):
        self._gaps(monkeypatch, AddressIndexCheck(missing=0, checked=40, unresolved=0))

        await housekeeping._tick()

        assert housekeeping.address_index_status()["state"] == "ok"

    async def test_a_window_with_nothing_to_check_reads_idle_not_ok(self, monkeypatch):
        """An empty window (ingestion stalled, or catching up on blocks older
        than the window) checked nothing, so it must not read as all clear."""
        self._gaps(monkeypatch, AddressIndexCheck(missing=0, checked=0, unresolved=0))

        await housekeeping._tick()

        assert housekeeping.address_index_status()["state"] == "idle"

    async def test_a_failed_check_reads_error_and_the_tick_goes_on(self, monkeypatch):
        self._gaps(monkeypatch, RuntimeError("clickhouse down"))

        await housekeeping._tick()

        assert housekeeping.address_index_status()["state"] == "error"

    async def test_the_check_is_throttled(self, monkeypatch):
        calls = self._gaps(monkeypatch, AddressIndexCheck(missing=0, checked=40, unresolved=0))

        await housekeeping._tick()
        await housekeeping._tick()

        assert len(calls) == 1

    async def test_a_check_turned_off_reads_disabled(self, monkeypatch):
        calls = self._gaps(monkeypatch, AddressIndexCheck(missing=0, checked=40, unresolved=0))
        monkeypatch.setattr(settings, "ADDRESS_INDEX_CHECK_INTERVAL_SECONDS", 0)

        await housekeeping._tick()

        assert calls == []
        assert housekeeping.address_index_status()["state"] == "disabled"

    async def test_the_check_does_not_run_without_cycle_detection(self, monkeypatch):
        calls = self._gaps(monkeypatch, AddressIndexCheck(missing=0, checked=40, unresolved=0))
        monkeypatch.setattr(settings, "CYCLE_DETECTION_ENABLED", False)

        await housekeeping._tick()

        assert calls == []
