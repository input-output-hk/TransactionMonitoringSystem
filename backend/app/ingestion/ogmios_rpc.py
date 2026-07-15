"""Shared low-level Ogmios JSON-RPC 2.0 wire framing.

One place for "send a request, await its response, parse the frame", so the live
chain-sync (``ogmios_client``), the mempool monitor (via the ``send_recv`` the
client injects into it), and the one-off address backfill (``address_backfill``)
all frame requests identically and parse large frames off the event loop.

Deliberately stateless: connection lifecycle, request-id sequencing, and any
telemetry stay with each owner (they hold different WebSockets and count ids
independently). This module is only the wire framing they share.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.config import settings


def jsonrpc_message(method: str, params: dict | None, request_id: str) -> str:
    """Serialise one JSON-RPC 2.0 request. ``params`` is omitted when falsy, as
    Ogmios methods without arguments (e.g. ``nextBlock``) take no ``params``."""
    msg: dict[str, object] = {"jsonrpc": "2.0", "method": method, "id": request_id}
    if params:
        msg["params"] = params
    return json.dumps(msg)


async def send_recv(ws: Any, method: str, params: dict | None, *, request_id: str) -> dict:
    """Send one JSON-RPC request on ``ws`` and return the parsed response.

    A busy block of Plutus txs serialises to tens of MB (the socket allows 64
    MB); frames above ``OGMIOS_PARSE_EXECUTOR_THRESHOLD_BYTES`` are parsed on the
    default executor so the shared event loop (API, WebSocket feed, mempool
    monitor, backfill) is not blocked for the parse duration. Smaller frames
    parse inline: the thread handoff costs more than the parse below the
    threshold.
    """
    await ws.send(jsonrpc_message(method, params, request_id))
    raw = await ws.recv()
    if len(raw) > settings.OGMIOS_PARSE_EXECUTOR_THRESHOLD_BYTES:
        return await asyncio.to_thread(json.loads, raw)
    return json.loads(raw)
