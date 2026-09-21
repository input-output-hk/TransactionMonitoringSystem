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
import logging
import re
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Ogmios renders a block's header fields before its `transactions` array, so the
# scan for a slot/id only has to read up to that key rather than the whole frame.
# The bound matters because this scan runs on frames json.loads has already
# refused: it is the fallback path for a multi-MB frame, not the hot path.
_BLOCK_KEY = '"block":'
_TRANSACTIONS_KEY = '"transactions"'
_BLOCK_ID_RE = re.compile(r'"id"\s*:\s*"([0-9a-f]{64})"')
_BLOCK_SLOT_RE = re.compile(r'"slot"\s*:\s*(\d+)')


class FrameUndecodableError(Exception):
    """A response frame arrived intact but could not be decoded into Python.

    Specifically the deep-nesting case: ``json.loads`` recurses once per nested
    container, so a deeply nested frame raises ``RecursionError`` however small
    the frame is. A Cardano native script is rendered by Ogmios as recursively
    nested JSON (``{"clause": "all", "from": [...]}``), and the ledger does not
    bound that depth, so any transaction can carry one deeper than the decoder
    survives. Observed on preprod 2026-09-21: one 205 KB block nested 10,774
    deep, which halted chain sync for 4.8 days because the checkpoint could
    never advance past it.

    Raising ``sys.setrecursionlimit`` is NOT the fix and was measured not to be:
    on 3.13 a depth-20,000 frame still raises with the limit at 5,000,000,
    because the decoder is bounded by the actual C stack rather than by that
    counter. The consequence for the handling below is that the failing depth is
    a property of the available stack, not a constant: it differs between
    platforms and between the main thread and an executor thread, so this is
    caught where it happens rather than predicted from a threshold.

    Raised as its own type so chain sync can quarantine exactly this block and
    keep monitoring the chain, rather than treating it as a transport fault and
    replaying the same undecodable frame forever. ``raw`` is retained so the
    caller can recover the block's identity lexically (see ``scan_block_point``)
    and record precisely what was skipped.
    """

    def __init__(self, method: str, raw: str, cause: BaseException):
        self.method = method
        self.raw = raw
        self.cause = cause
        super().__init__(f"{method}: response frame could not be decoded ({cause!r})")


def scan_block_point(raw: str) -> tuple[str | None, int | None]:
    """Recover ``(block_id, slot)`` from an undecodable frame, without parsing.

    The point of this is to let a quarantined block still advance the sync
    checkpoint: skipping it needs its own point, and by definition the frame it
    came in cannot be parsed to read one. Scanning is confined to the header
    segment between ``"block":`` and ``"transactions"``, so a transaction id or
    a datum's ``slot`` field further down cannot be mistaken for the block's.

    Returns ``(None, None)`` when the segment is absent or malformed; the caller
    must treat that as "cannot safely skip" rather than guessing a point.
    """
    start = raw.find(_BLOCK_KEY)
    if start < 0:
        return None, None
    end = raw.find(_TRANSACTIONS_KEY, start)
    header = raw[start:end] if end > start else raw[start:]
    block_id = _BLOCK_ID_RE.search(header)
    slot = _BLOCK_SLOT_RE.search(header)
    return (
        block_id.group(1) if block_id else None,
        int(slot.group(1)) if slot else None,
    )


def max_nesting_depth(raw: str) -> int:
    """Maximum bracket nesting in ``raw``, counted lexically.

    Diagnostics only, for the quarantine record: it answers "how deep was it"
    on a frame that cannot be parsed to find out. String contents are skipped so
    a bracket inside a JSON string does not inflate the count.
    """
    depth = deepest = 0
    in_string = escaped = False
    for ch in raw:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in "]}":
            depth -= 1
    return deepest


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
    # A frame nested past CPython's recursion limit raises RecursionError here
    # rather than returning, and retrying re-reads the identical frame, so the
    # sync cannot make progress on its own. Re-raise as FrameUndecodableError to
    # separate "this specific frame is undecodable" from the transport faults
    # the reconnect loop is built to retry; chain sync quarantines the former.
    try:
        if len(raw) > settings.OGMIOS_PARSE_EXECUTOR_THRESHOLD_BYTES:
            resp = await asyncio.to_thread(json.loads, raw)
        else:
            resp = json.loads(raw)
    except RecursionError as e:
        raise FrameUndecodableError(method, raw, e) from e
    # This assumes strict request/response ordering on a single socket. If a prior
    # call was cancelled after send() but before recv(), a reused socket can hand
    # back the stale response and desync every later call. We can't recover here
    # (the framing is stateless by design), but a mismatched id is the signature of
    # that desync, so surface it rather than let it corrupt results silently. Not
    # fatal: some responses may legitimately omit the id, so only a present-and-
    # different id warns.
    resp_id = resp.get("id") if isinstance(resp, dict) else None
    if resp_id is not None and resp_id != request_id:
        logger.warning(
            "Ogmios JSON-RPC id mismatch for %s: sent %r, received %r "
            "(possible socket desync from a cancelled prior call)",
            method,
            request_id,
            resp_id,
        )
    return resp
