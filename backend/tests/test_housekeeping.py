"""app.tasks.housekeeping runs independently of ANALYSIS_ENGINE_ENABLED: the
stale-PENDING DROPPED sweep, retention, and auth purge used to live inside
the analysis engine's loop, so disabling scoring silently disabled all of
them too (review finding). These tests drive one _tick() directly.
"""

import pytest

from app.config import settings
from app.tasks import housekeeping

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_state():
    housekeeping._last_retention_sweep = 0.0
    yield
    housekeeping._last_retention_sweep = 0.0


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

    from app.auth import sessions as auth_sessions, tokens as auth_tokens
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

    from app.auth import sessions as auth_sessions, tokens as auth_tokens
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

    from app.auth import sessions as auth_sessions, tokens as auth_tokens
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
