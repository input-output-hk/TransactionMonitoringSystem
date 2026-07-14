"""WebSocket router for real-time transaction updates.

Broadcast is decoupled from ingestion via per-client bounded queues: the
chain-sync and mempool paths previously awaited ``send_json`` on every
client sequentially, so one slow client with a full TCP buffer stalled
block ingestion for the whole process. ``broadcast`` now only does a
non-blocking enqueue (dropping the OLDEST event when a client's queue is
full — a lagging dashboard wants the newest state, and the WS feed is a
live view, not the system of record), and a per-client sender task drains
the queue at whatever pace the client can sustain.

The sender task is the ONLY writer on each socket: even the keepalive pong
goes through the queue, because two tasks calling ``send_json`` on one
connection interleave frames (review finding).

Handshake rate limiting lives HERE, not in the HTTP middleware:
BaseHTTPMiddleware never dispatches ``websocket`` scopes, so a path
exemption there was dead code while the handshake stayed unthrottled.
"""

import asyncio
import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app import net
from app.auth import _dev_mode, is_valid_api_key
from app.config import settings
from app.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

router = APIRouter()

# Application close codes in the 4000-4999 private-use range (RFC 6455
# section 7.4.2), mirroring the HTTP statuses they correspond to; 1011 is
# the registered server-error code.
WS_CLOSE_FORBIDDEN = 4403  # invalid/missing api_key (mirrors HTTP 403)
WS_CLOSE_OVERLOADED = 4429  # connection cap or handshake rate limit (HTTP 429)
WS_CLOSE_INTERNAL_ERROR = 1011  # RFC 6455 internal-error close
# Disallowed Origin header on the upgrade. 1008 is the registered
# policy-violation code (RFC 6455 section 7.4.1), the standard close for
# origin-based rejection.
WS_CLOSE_POLICY_VIOLATION = 1008

# Global list of active connections (set from main.py; also used for the
# /health connection count)
active_connections: List[WebSocket] = []

# Per-client outbound queues, keyed by the WebSocket object.
_client_queues: Dict[WebSocket, asyncio.Queue] = {}

# Handshake limiter: repeated rejected upgrade attempts were unthrottled
# (the HTTP middleware never sees websocket scopes). Keyed on the validated
# client IP from app.net.
_handshake_limiter = RateLimiter(
    max_requests=settings.WS_HANDSHAKE_RATE_LIMIT_REQUESTS,
    window_seconds=settings.WS_HANDSHAKE_RATE_LIMIT_WINDOW_SECONDS,
)


def set_active_connections(connections: List[WebSocket]):
    """Set the active WebSocket connections list"""
    global active_connections
    active_connections = connections


def _enqueue(queue: asyncio.Queue, payload: dict) -> None:
    """Non-blocking enqueue; a full queue drops its oldest entry first."""
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()  # drop the oldest event
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # racing sender; the next event will get through


async def broadcast(payload: dict) -> None:
    """Enqueue ``payload`` for every connected client without blocking.

    Never awaits network I/O: ingestion latency must not depend on any
    client's receive rate.
    """
    for queue in list(_client_queues.values()):
        _enqueue(queue, payload)


async def _sender(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Drain one client's queue at the client's own pace.

    A send failure closes the connection and removes the subscriber:
    leaving the entry registered after the sender dies produced a zombie
    client that silently received nothing (review finding).
    """
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "WebSocket sender failed; closing connection",
            exc_info=True,
        )
        _cleanup(websocket)
        try:
            await websocket.close(code=WS_CLOSE_INTERNAL_ERROR)
        except Exception:
            pass


def _cleanup(websocket: WebSocket) -> None:
    _client_queues.pop(websocket, None)
    if websocket in active_connections:
        active_connections.remove(websocket)


async def _reject(websocket: WebSocket, code: int) -> None:
    """Accept the handshake, then immediately close with ``code``.

    Accept-first is deliberate: ``close()`` before ``accept()`` surfaces to
    real clients as a bare HTTP 403 on the upgrade, with no close frame at
    all, so a reconnecting dashboard cannot distinguish rate-limited (back
    off) from bad key (stop retrying). Accepting costs one completed
    upgrade round-trip but makes the application close code observable.
    The socket is never registered in active_connections, so a rejected
    handshake receives no broadcast data.
    """
    await websocket.accept()
    await websocket.close(code=code)


def _origin_allowed(origin: Optional[str]) -> bool:
    """Cross-site WebSocket hijacking guard for the live alert feed.

    Browsers always attach an Origin header to WebSocket upgrades, so a
    present-but-unlisted Origin means some other site's page is opening
    the feed with the victim's network position. When API keys are
    configured the key remains the primary gate; this check is what
    protects dev mode (API_KEYS empty + TMS_ALLOW_DEV_MODE), where the
    endpoint is otherwise open. Non-browser clients send no Origin, so an
    absent header stays allowed. Reuses the CORS allowlist (read-only):
    "*" (or an empty list, i.e. nothing configured) means the deployment
    is not origin-restricted and every Origin is accepted.
    """
    if origin is None:
        return True
    allowed = settings.cors_allow_origins_list
    if not allowed or "*" in allowed:
        return True
    return origin in allowed


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    api_key: Optional[str] = Query(None, alias="api_key"),
):
    """WebSocket endpoint for real-time transaction updates.

    Authentication: pass ?api_key=<key> as a query parameter when API_KEYS is
    configured.  In dev mode (API_KEYS not set) the endpoint is open to all
    clients.  WebSocket upgrades cannot carry custom headers from browsers, so
    a query-parameter key is the standard approach. Note query strings can
    land in proxy/access logs: use a dedicated key for dashboards so it can
    be rotated independently of automation keys.
    """
    if settings.RATE_LIMIT_ENABLED:
        ip = net.client_ip(websocket)
        allowed, _retry_after = await _handshake_limiter.check(f"ws:{ip or 'unknown'}")
        if not allowed:
            await _reject(websocket, WS_CLOSE_OVERLOADED)
            return

    if not _origin_allowed(websocket.headers.get("origin")):
        await _reject(websocket, WS_CLOSE_POLICY_VIOLATION)
        return

    if not _dev_mode and not is_valid_api_key(api_key):
        await _reject(websocket, WS_CLOSE_FORBIDDEN)
        return

    # Cap concurrent connections to prevent resource exhaustion
    if len(active_connections) >= settings.WS_MAX_CONNECTIONS:
        await _reject(websocket, WS_CLOSE_OVERLOADED)
        return

    await websocket.accept()
    active_connections.append(websocket)
    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.WS_CLIENT_QUEUE_SIZE)
    _client_queues[websocket] = queue
    sender = asyncio.create_task(_sender(websocket, queue))
    logger.info(f"WebSocket client connected. Total connections: {len(active_connections)}")

    try:
        while True:
            # Keep connection alive; the pong rides the queue so the sender
            # task stays the only writer on this socket.
            await websocket.receive_text()
            _enqueue(queue, {"type": "pong", "message": "connected"})
    except WebSocketDisconnect:
        logger.info(
            f"WebSocket client disconnected. Total connections: {len(active_connections) - 1}"
        )
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        sender.cancel()
        _cleanup(websocket)
