"""WebSocket router for real-time transaction updates"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.auth import _dev_mode, _valid_keys

logger = logging.getLogger(__name__)

router = APIRouter()

# Global list of active connections (set from main.py)
active_connections: List[WebSocket] = []


def set_active_connections(connections: List[WebSocket]):
    """Set the active WebSocket connections list"""
    global active_connections
    active_connections = connections


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
    if not _dev_mode and api_key not in _valid_keys:
        await websocket.close(code=4403)
        return

    # Cap concurrent connections to prevent resource exhaustion
    if len(active_connections) >= 100:
        await websocket.close(code=4429)
        return

    await websocket.accept()
    active_connections.append(websocket)
    logger.info(f"WebSocket client connected. Total connections: {len(active_connections)}")

    try:
        while True:
            # Keep connection alive and handle any client messages
            data = await websocket.receive_text()
            # Echo back or handle client requests
            await websocket.send_json({"type": "pong", "message": "connected"})
    except WebSocketDisconnect:
        active_connections.remove(websocket)
        logger.info(f"WebSocket client disconnected. Total connections: {len(active_connections)}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if websocket in active_connections:
            active_connections.remove(websocket)
