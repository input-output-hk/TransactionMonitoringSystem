"""WebSocket router for real-time transaction updates.

Broadcast is decoupled from ingestion via per-client bounded queues: the
chain-sync and mempool paths previously awaited ``send_json`` on every
client sequentially, so one slow client with a full TCP buffer stalled
block ingestion for the whole process. ``broadcast`` now only does a
non-blocking enqueue (dropping the OLDEST event when a client's queue is
full — a lagging dashboard wants the newest state, and the WS feed is a
live view, not the system of record), and a per-client sender task drains
the queue at whatever pace the client can sustain.
"""

import asyncio
import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.auth import _dev_mode, is_valid_api_key
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Global list of active connections (set from main.py; also used for the
# /health connection count)
active_connections: List[WebSocket] = []

# Per-client outbound queues, keyed by the WebSocket object.
_client_queues: Dict[WebSocket, asyncio.Queue] = {}


def set_active_connections(connections: List[WebSocket]):
    """Set the active WebSocket connections list"""
    global active_connections
    active_connections = connections


async def broadcast(payload: dict) -> None:
    """Enqueue ``payload`` for every connected client without blocking.

    Never awaits network I/O: ingestion latency must not depend on any
    client's receive rate. A full queue drops its oldest entry first.
    """
    for queue in list(_client_queues.values()):
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


async def _sender(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Drain one client's queue at the client's own pace."""
    while True:
        payload = await queue.get()
        await websocket.send_json(payload)


def _cleanup(websocket: WebSocket) -> None:
    _client_queues.pop(websocket, None)
    if websocket in active_connections:
        active_connections.remove(websocket)


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    api_key: Optional[str] = Query(None, alias="api_key"),
):
    """WebSocket endpoint for real-time transaction updates.

    Authentication: pass ?api_key=<key> as a query parameter when API_KEYS is
    configured.  In dev mode (API_KEYS not set) the endpoint is open to all
    clients.  WebSocket upgrades cannot carry custom headers from browsers, so
    a query-parameter key is the standard approach.
    """
    if not _dev_mode and not is_valid_api_key(api_key):
        await websocket.close(code=4403)
        return

    # Cap concurrent connections to prevent resource exhaustion
    if len(active_connections) >= settings.WS_MAX_CONNECTIONS:
        await websocket.close(code=4429)
        return

    await websocket.accept()
    active_connections.append(websocket)
    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.WS_CLIENT_QUEUE_SIZE)
    _client_queues[websocket] = queue
    sender = asyncio.create_task(_sender(websocket, queue))
    logger.info(f"WebSocket client connected. Total connections: {len(active_connections)}")

    try:
        while True:
            # Keep connection alive and handle any client messages
            await websocket.receive_text()
            await websocket.send_json({"type": "pong", "message": "connected"})
    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected. Total connections: {len(active_connections) - 1}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        sender.cancel()
        _cleanup(websocket)
