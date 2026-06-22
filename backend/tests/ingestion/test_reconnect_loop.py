"""The shared resilient-reconnect loop (resilience.run_with_reconnect) used by
the chain-sync and mempool Ogmios clients.

No test covered this outer loop before it was extracted, so this pins its
contract: the circuit-breaker-open gate (sleep + skip the attempt), the
success path (mark connected, record success, reset backoff, run the session),
both failure branches (connection-lost and unexpected), and the always-run
close() in finally.
"""

from __future__ import annotations

import pytest

from app.ingestion import resilience
from app.ingestion.resilience import run_with_reconnect

pytestmark = pytest.mark.asyncio


class _Breaker:
    def __init__(self, can_attempt_results=None):
        self._can = list(can_attempt_results or [])
        self.successes = 0
        self.failures = 0

    def can_attempt(self) -> bool:
        return self._can.pop(0) if self._can else True

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


class _Backoff:
    def __init__(self) -> None:
        self.resets = 0
        self.waits = 0

    def reset(self) -> None:
        self.resets += 1

    async def wait(self) -> None:
        self.waits += 1


class _Harness:
    """Drives run_with_reconnect for a bounded number of while-iterations."""

    def __init__(self, *, max_iters, session_exc=None, can_attempt=None):
        self.max_iters = max_iters
        self.session_exc = session_exc
        self.breaker = _Breaker(can_attempt)
        self.backoff = _Backoff()
        self.connected: list[bool] = []
        self.connects = 0
        self.sessions = 0
        self.closes = 0
        self._iters = 0

    def is_running(self) -> bool:
        self._iters += 1
        return self._iters <= self.max_iters

    async def connect(self):
        self.connects += 1
        return object()

    async def run_session(self, session) -> None:
        self.sessions += 1
        if self.session_exc is not None:
            raise self.session_exc

    def on_connected(self, value: bool) -> None:
        self.connected.append(value)

    async def close(self) -> None:
        self.closes += 1

    async def run(self, poll_seconds: int = 10) -> None:
        await run_with_reconnect(
            name="test",
            is_running=self.is_running,
            breaker=self.breaker,
            backoff=self.backoff,
            connect=self.connect,
            run_session=self.run_session,
            on_connected=self.on_connected,
            close=self.close,
            poll_seconds=poll_seconds,
        )


async def test_success_path_resets_and_records():
    h = _Harness(max_iters=1)
    await h.run()
    assert h.connects == 1 and h.sessions == 1
    assert h.connected == [True]
    assert (h.breaker.successes, h.breaker.failures) == (1, 0)
    assert (h.backoff.resets, h.backoff.waits) == (1, 0)
    assert h.closes == 1  # finally always closes


async def test_connection_lost_branch_backs_off():
    h = _Harness(max_iters=1, session_exc=OSError("dropped"))
    await h.run()
    assert h.connected == [True, False]  # connected, then marked down on failure
    assert (h.breaker.successes, h.breaker.failures) == (1, 1)
    assert h.backoff.waits == 1
    assert h.closes == 1


async def test_unexpected_error_branch_backs_off():
    h = _Harness(max_iters=1, session_exc=ValueError("boom"))
    await h.run()
    assert h.breaker.failures == 1 and h.backoff.waits == 1
    assert h.connected[-1] is False
    assert h.closes == 1


async def test_breaker_open_gate_sleeps_and_skips_connect(monkeypatch):
    slept: list[int] = []

    async def _fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(resilience.asyncio, "sleep", _fake_sleep)
    # iter1: breaker open -> gate (sleep, continue, no connect/close);
    # iter2: breaker closed -> success; iter3: is_running False -> stop.
    h = _Harness(max_iters=2, can_attempt=[False, True])
    await h.run(poll_seconds=7)
    assert slept == [7]            # gated once with poll_seconds
    assert h.connects == 1         # connect skipped on the gated iteration
    assert h.breaker.successes == 1
    assert h.closes == 1           # only the connected iteration reaches finally
