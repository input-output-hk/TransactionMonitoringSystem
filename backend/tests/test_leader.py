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
