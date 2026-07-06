"""The delivery concurrency limiter bounds simultaneous sends so a burst
(backlog drain / spam wave) cannot open hundreds of SMTP/webhook connections
at once and trip the endpoint's rate limits."""

import asyncio

import pytest

from app import notifications
from app.config import settings

pytestmark = pytest.mark.asyncio


async def test_deliveries_are_concurrency_bounded(monkeypatch):
    limit = 3
    monkeypatch.setattr(settings, "NOTIFY_MAX_CONCURRENT_DELIVERIES", limit)
    # Fresh semaphore bound to this test's running loop.
    notifications.set_main_loop(asyncio.get_running_loop())

    state = {"in_flight": 0, "peak": 0}

    async def fake_dispatch(payload, dispatches, attachments=None):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0.01)  # hold the slot so overlap is observable
        state["in_flight"] -= 1
        return True

    monkeypatch.setattr(notifications.dispatcher, "dispatch", fake_dispatch)
    monkeypatch.setattr(notifications.postgres, "already_notified",
                        lambda *a, **k: _false())
    monkeypatch.setattr(notifications.postgres, "claim_notification",
                        lambda *a, **k: _true())

    # Fire far more deliveries than the limit, concurrently.
    await asyncio.gather(*(
        notifications._deliver_with_dedup("preprod", f"tx{i}", "Critical", {}, [("email", {})])
        for i in range(20)
    ))
    assert state["peak"] <= limit, f"peak concurrency {state['peak']} exceeded limit {limit}"


async def _false():
    return False


async def _true():
    return True
