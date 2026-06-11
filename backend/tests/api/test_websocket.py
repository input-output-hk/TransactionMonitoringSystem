"""WebSocket hardening: single-writer pong, sender-death cleanup,
handshake rate limit, auth close codes.

Two tasks calling send_json on one socket interleave frames, and a dead
sender used to leave a zombie subscriber receiving nothing (review
findings); the handshake was unthrottled because the HTTP rate-limit
middleware never sees websocket scopes.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.rate_limit import RateLimiter
from app.routers import websocket as ws_mod


@pytest.fixture
def dev_mode(monkeypatch):
    from app.routers import websocket
    monkeypatch.setattr(websocket, "_dev_mode", True)


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


class TestPongViaQueue:
    def test_ping_gets_pong_through_sender_queue(self, client, dev_mode):
        with client.websocket_connect("/ws") as ws:
            ws.send_text("ping")
            msg = ws.receive_json()
        assert msg == {"type": "pong", "message": "connected"}


class TestSenderFailureCleanup:
    def test_dead_sender_unregisters_and_closes(self, dev_mode):
        fake_ws = AsyncMock()
        fake_ws.send_json.side_effect = RuntimeError("transport broken")
        queue: asyncio.Queue = asyncio.Queue()
        ws_mod._client_queues[fake_ws] = queue

        async def run():
            await queue.put({"type": "x"})
            # Must return (not raise): the failure is handled, not leaked.
            await ws_mod._sender(fake_ws, queue)

        try:
            asyncio.run(run())
            assert fake_ws not in ws_mod._client_queues
            fake_ws.close.assert_awaited_once_with(
                code=ws_mod.WS_CLOSE_INTERNAL_ERROR
            )
        finally:
            ws_mod._client_queues.pop(fake_ws, None)

    def test_cancellation_still_propagates(self, dev_mode):
        fake_ws = AsyncMock()
        queue: asyncio.Queue = asyncio.Queue()

        async def run():
            task = asyncio.create_task(ws_mod._sender(fake_ws, queue))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())


class TestHandshakeRateLimit:
    def test_third_connect_rejected(self, client, dev_mode, monkeypatch):
        monkeypatch.setattr(
            ws_mod, "_handshake_limiter",
            RateLimiter(max_requests=2, window_seconds=60),
        )
        for _ in range(2):
            with client.websocket_connect("/ws"):
                pass
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws"):
                pass
        assert exc.value.code == ws_mod.WS_CLOSE_OVERLOADED


class TestAuthCloseCode:
    def test_invalid_key_forbidden(self, client, monkeypatch):
        monkeypatch.setattr(ws_mod, "_dev_mode", False)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws?api_key=wrong"):
                pass
        assert exc.value.code == ws_mod.WS_CLOSE_FORBIDDEN
