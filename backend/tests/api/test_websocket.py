"""WebSocket hardening: single-writer pong, sender-death cleanup,
handshake rate limit, Origin allowlist, auth close codes.

Two tasks calling send_json on one socket interleave frames, and a dead
sender used to leave a zombie subscriber receiving nothing (review
findings); the handshake was unthrottled because the HTTP rate-limit
middleware never sees websocket scopes. Rejections accept the upgrade
first so the close code is observable (close-before-accept surfaces as a
bare HTTP 403, indistinguishable from every other rejection).
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


@pytest.fixture(autouse=True)
def fresh_handshake_limiter(monkeypatch):
    """Each test gets its own limiter: the module-level one is keyed on the
    client IP ('testclient' for every test here), so connects would
    accumulate across tests and trip the shared window."""
    monkeypatch.setattr(
        ws_mod,
        "_handshake_limiter",
        RateLimiter(max_requests=100, window_seconds=60),
    )


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def assert_rejected_after_accept(client, ws_path, expected_code, headers=None):
    """The handshake must complete (accept-first) and the very next receive
    must surface the application close code."""
    with client.websocket_connect(ws_path, headers=headers or {}) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == expected_code


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
            fake_ws.close.assert_awaited_once_with(code=ws_mod.WS_CLOSE_INTERNAL_ERROR)
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
            ws_mod,
            "_handshake_limiter",
            RateLimiter(max_requests=2, window_seconds=60),
        )
        for _ in range(2):
            with client.websocket_connect("/ws"):
                pass
        # Accept-first: the upgrade succeeds, then the 4429 close arrives,
        # so a reconnecting client can tell "back off" from "bad key".
        assert_rejected_after_accept(client, "/ws", ws_mod.WS_CLOSE_OVERLOADED)


class TestAuthCloseCode:
    def test_invalid_key_forbidden(self, client, monkeypatch):
        monkeypatch.setattr(ws_mod, "_dev_mode", False)
        assert_rejected_after_accept(client, "/ws?api_key=wrong", ws_mod.WS_CLOSE_FORBIDDEN)

    def test_rejected_socket_is_never_registered(self, client, monkeypatch):
        """Accept-first must not leak the rejected socket into the broadcast
        registry: it gets no queue and no active_connections slot."""
        monkeypatch.setattr(ws_mod, "_dev_mode", False)
        before = len(ws_mod.active_connections)
        assert_rejected_after_accept(client, "/ws?api_key=wrong", ws_mod.WS_CLOSE_FORBIDDEN)
        assert len(ws_mod.active_connections) == before
        assert len(ws_mod._client_queues) == 0


class TestOriginCheck:
    """Cross-site WebSocket hijacking guard: with a restrictive CORS
    allowlist, an upgrade carrying an unlisted Origin is refused even in
    dev mode (no API keys), where the Origin check is the only gate."""

    @pytest.fixture
    def restrictive_origins(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "http://dashboard.example")

    def test_disallowed_origin_rejected(self, client, dev_mode, restrictive_origins):
        assert_rejected_after_accept(
            client,
            "/ws",
            ws_mod.WS_CLOSE_POLICY_VIOLATION,
            headers={"Origin": "http://evil.example"},
        )

    def test_allowed_origin_accepted(self, client, dev_mode, restrictive_origins):
        with client.websocket_connect("/ws", headers={"Origin": "http://dashboard.example"}) as ws:
            ws.send_text("ping")
            assert ws.receive_json()["type"] == "pong"

    def test_absent_origin_accepted(self, client, dev_mode, restrictive_origins):
        # Non-browser clients (CLI tooling, monitors) send no Origin header;
        # the guard only targets browser-initiated cross-site upgrades.
        with client.websocket_connect("/ws") as ws:
            ws.send_text("ping")
            assert ws.receive_json()["type"] == "pong"

    def test_wildcard_cors_accepts_any_origin(self, client, dev_mode, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "*")
        with client.websocket_connect("/ws", headers={"Origin": "http://anywhere.example"}) as ws:
            ws.send_text("ping")
            assert ws.receive_json()["type"] == "pong"
