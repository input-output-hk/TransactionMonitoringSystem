"""Single-instance leader guard (app.leader): a second live process would
double-insert transactions and race the Ogmios sync checkpoint, so only one
process may hold the advisory lock at a time. Hermetic: asyncpg.connect is
replaced with a fake that models pg_try_advisory_lock's shared, session-scoped
semantics across the two "connections" in play.
"""

import pytest

from app import leader

pytestmark = pytest.mark.asyncio


class _FakeConn:
    """One fake PG session. ``_held`` is a class-level set shared across all
    instances, modelling the lock being visible server-side to every session."""

    _held: set = set()

    def __init__(self):
        self.closed = False

    async def fetchval(self, query, key):
        assert "pg_try_advisory_lock" in query
        if key in _FakeConn._held:
            return False
        _FakeConn._held.add(key)
        return True

    async def execute(self, query, key):
        assert "pg_advisory_unlock" in query
        _FakeConn._held.discard(key)

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _FakeConn._held = set()

    async def fake_connect(**kwargs):
        return _FakeConn()

    monkeypatch.setattr(leader.asyncpg, "connect", fake_connect)
    leader._conn = None
    yield
    leader._conn = None
    _FakeConn._held = set()


async def test_try_acquire_succeeds_when_unheld():
    assert await leader.try_acquire() is True
    assert leader.is_leader() is True


async def test_try_acquire_is_idempotent_once_leader():
    assert await leader.try_acquire() is True
    first_conn = leader._conn
    assert await leader.try_acquire() is True
    assert leader._conn is first_conn  # no second connection opened


async def test_try_acquire_fails_when_already_held_elsewhere():
    _FakeConn._held.add(leader.settings.LEADER_LOCK_KEY)
    assert await leader.try_acquire() is False
    assert leader.is_leader() is False


async def test_release_frees_the_lock_for_another_instance():
    await leader.try_acquire()
    assert leader.is_leader() is True
    await leader.release()
    assert leader.is_leader() is False
    assert leader.settings.LEADER_LOCK_KEY not in _FakeConn._held
    # A standby can now win it.
    assert await leader.try_acquire() is True


async def test_release_is_a_noop_when_never_leader():
    await leader.release()  # must not raise
    assert leader.is_leader() is False


class TestStandbyPromotion:
    """main._standby_promote must never give up on an error: a transient PG
    blip during a probe, or a failed duty startup after winning the lock,
    must not leave the fleet with a silent permanent standby or a do-nothing
    leader (review finding)."""

    @pytest.fixture(autouse=True)
    def _fast_retry(self, monkeypatch):
        from app import main as app_main
        monkeypatch.setattr(app_main.settings, "LEADER_LOCK_RETRY_SECONDS", 0)

    async def test_probe_error_does_not_kill_the_retry_loop(self, monkeypatch):
        from unittest.mock import AsyncMock

        from app import main as app_main

        calls = {"n": 0}

        async def flaky_acquire():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("pg connection blip")
            return calls["n"] >= 3  # held elsewhere on 2, won on 3

        monkeypatch.setattr(app_main.leader, "try_acquire", flaky_acquire)
        started = AsyncMock()
        monkeypatch.setattr(app_main, "_start_leader_duties", started)

        await app_main._standby_promote()

        assert calls["n"] == 3
        started.assert_awaited_once()

    async def test_failed_promotion_unwinds_releases_and_retries(self, monkeypatch):
        from unittest.mock import AsyncMock

        from app import main as app_main

        monkeypatch.setattr(app_main.leader, "try_acquire", AsyncMock(return_value=True))
        release = AsyncMock()
        monkeypatch.setattr(app_main.leader, "release", release)
        stop = AsyncMock()
        monkeypatch.setattr(app_main, "_stop_leader_duties", stop)

        attempts = {"n": 0}

        async def start_duties():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("ogmios connect failed mid-promotion")

        monkeypatch.setattr(app_main, "_start_leader_duties", start_duties)

        await app_main._standby_promote()

        assert attempts["n"] == 2  # failed once, succeeded on the retry
        stop.assert_awaited_once()      # partial start unwound
        release.assert_awaited_once()   # lock freed for another instance
