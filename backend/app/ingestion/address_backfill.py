"""On-demand historical backfill of one address's transactions.

The node syncs tip-forward with no historical backfill, so a contract whose
activity predates this deployment's sync start has no rows in ClickHouse and
cannot be onboarded (the clustering sidecar's onboarding fails with "no
transactions in this instance's data"). This module fills that gap **without**
lowering detection fidelity:

  1. Kupo (the address→tx index the node lacks) supplies the *block points*
     (slot + header hash) of the transactions touching the address, newest-first
     and capped at ``max_txs`` (see ``kupo_client.py``).
  2. A standalone Ogmios chain-sync session re-fetches exactly those blocks and
     runs them through the canonical ``ogmios_parser`` + ``resolve_input_amounts``
     + ``insert_transactions_batch_async``. The rows written are byte-for-byte the
     same shape as live-synced ones (real fee, size, redeemers, chain-time), so
     backfilled transactions look identical to the detectors.

This session is deliberately isolated from the live chain-sync loop: it opens its
own WebSocket and NEVER calls ``save_sync_point``, so it cannot move the live
ingestion checkpoint. Inserts are idempotent (ReplacingMergeTree), so a re-run or
a mid-run reconnect is harmless.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import websockets

from app.config import settings
from app.db import clickhouse
from app.ingestion.chain_time import SlotTimeConverter
from app.ingestion.input_enrichment import resolve_input_amounts
from app.ingestion.kupo_client import KupoClient
from app.ingestion.ogmios_parser import parse_ogmios_transaction
from app.models.transaction import NormalizedTransaction

logger = logging.getLogger(__name__)


def _noop(_: str) -> None:  # pragma: no cover - default progress sink
    pass


@dataclass(slots=True)
class BackfillResult:
    """Outcome of a backfill run."""

    address: str
    requested_txs: int  # distinct transactions Kupo pointed us at (after the max_txs cap)
    blocks_scanned: int  # blocks read from chain-sync between the anchor and the last target
    txs_ingested: int  # transactions parsed and inserted
    # Target hashes never seen in the scanned range (e.g. rolled back since Kupo
    # indexed them, or the single block at Kupo's index floor when no earlier
    # checkpoint exists to anchor before it).
    missing_tx_hashes: list[str]


class _OgmiosReader:
    """A minimal Ogmios JSON-RPC 2.0 chain-sync reader over an open WebSocket.

    A deliberately separate implementation from ``ChainSyncClient``: that client
    owns the live checkpoint and lifecycle side effects, none of which a read-only
    backfill may touch. This reader only intersects and pulls blocks forward.
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._id = 0

    async def _send_recv(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg: dict[str, object] = {"jsonrpc": "2.0", "method": method, "id": self._id}
        if params:
            msg["params"] = params
        await self._ws.send(json.dumps(msg))
        raw = await self._ws.recv()
        return json.loads(raw)

    async def slot_time_converter(self) -> SlotTimeConverter | None:
        """Build the slot→UTC converter so backfilled ``timestamp`` is chain-time,
        matching live ingestion. Best-effort: None falls back to wall clock (only
        skews the two time-of-day shape features, and only if this query fails)."""
        try:
            start = await self._send_recv("queryNetwork/startTime")
            eras = await self._send_recv("queryLedgerState/eraSummaries")
            return SlotTimeConverter.from_ogmios(start.get("result"), eras.get("result"))
        except Exception as exc:  # pragma: no cover - defensive; query is non-critical
            logger.warning("Backfill: slot-time query failed (%s); using wall clock", exc)
            return None

    async def find_intersection(self, points: list[dict | str]) -> None:
        resp = await self._send_recv("findIntersection", {"points": points})
        if "error" in resp:
            raise BackfillError(f"findIntersection failed: {resp['error']}")

    async def next_block(self) -> tuple[str, Any]:
        """``(direction, payload)`` for one ``nextBlock``: ``("forward", block)`` or
        ``("backward", point)``. Ogmios long-polls at the tip, but the backfill
        stops once it passes the last target slot, so it never blocks there."""
        resp = await self._send_recv("nextBlock")
        result = resp.get("result", {})
        return result.get("direction", ""), (
            result.get("block") if result.get("direction") == "forward" else result.get("point")
        )


class BackfillError(RuntimeError):
    """A backfill run failed (Ogmios intersection/parse/insert)."""


async def backfill_address(
    address: str,
    *,
    network: str,
    max_txs: int | None = None,
    progress: Callable[[str], None] = _noop,
) -> BackfillResult:
    """Backfill up to ``max_txs`` of the latest transactions for ``address``.

    Raises ``KupoUnavailable`` when Kupo is not configured and ``BackfillError`` on
    an Ogmios failure. Returns a summary (see ``BackfillResult``); an empty Kupo
    result is a normal "nothing to do", not an error.
    """
    kupo = KupoClient()
    points = await kupo.address_tx_points(address, max_txs=max_txs)
    if not points:
        progress(f"Kupo has no matches for {address[:24]}…; nothing to backfill")
        return BackfillResult(address, 0, 0, 0, [])

    needed = {p.tx_hash for p in points}
    earliest = min(p.slot for p in points)
    latest = max(p.slot for p in points)
    progress(
        f"backfilling {len(needed)} txs for {address[:24]}… "
        f"(slots {earliest} to {latest})"
    )

    # Intersect at a point strictly before the earliest target so the forward walk
    # re-covers it (findIntersection positions the read head AT the point; the
    # target block is delivered only when the intersection is its ancestor).
    anchor = await kupo.ancestor_point(earliest)
    if anchor is not None:
        intersection: dict | str = {"slot": anchor.slot, "id": anchor.header_hash}
    else:
        # No earlier checkpoint (earliest target sits at Kupo's index floor).
        # Intersect at the earliest target itself rather than risk a genesis-to-
        # latest rescan; forward delivery starts at its successor, so that one
        # block's txs may be missed and are reported in missing_tx_hashes.
        earliest_point = next(p for p in points if p.slot == earliest)
        intersection = {"slot": earliest_point.slot, "id": earliest_point.header_hash}
        progress("no pre-earliest checkpoint; the single earliest block may be skipped")

    seen: set[str] = set()
    blocks_scanned = 0
    txs_ingested = 0

    async with websockets.connect(
        settings.OGMIOS_WS_URL,
        ping_interval=settings.OGMIOS_HEARTBEAT_INTERVAL,
        ping_timeout=settings.OGMIOS_HEARTBEAT_TIMEOUT,
        max_size=settings.OGMIOS_WS_MAX_FRAME_BYTES,
    ) as ws:
        reader = _OgmiosReader(ws)
        converter = await reader.slot_time_converter()
        await reader.find_intersection([intersection])

        # The walk terminates: target blocks all have slot <= latest and the chain
        # advances strictly, so a block with slot > latest is reached in finitely
        # many steps even if some target hash never appears (rolled back).
        while needed - seen:
            direction, payload = await reader.next_block()
            if direction == "backward":
                # Historical/immutable region; a re-org near the tip only re-delivers
                # blocks we then re-insert idempotently. Keep reading forward.
                continue
            if direction != "forward" or not payload:
                continue
            block = payload
            slot = block.get("slot")
            if slot is None:  # Byron epoch-boundary block: no slot, no txs
                continue
            if slot > latest:
                break

            blocks_scanned += 1
            txs_ingested += await _ingest_block_targets(
                block, slot, needed, seen, network, converter
            )

    missing = sorted(needed - seen)
    progress(
        f"backfill done: {txs_ingested} txs ingested from {blocks_scanned} blocks; "
        f"{len(missing)} target(s) not found"
    )
    return BackfillResult(address, len(needed), blocks_scanned, txs_ingested, missing)


async def _ingest_block_targets(
    block: dict,
    slot: int,
    needed: set[str],
    seen: set[str],
    network: str,
    converter: SlotTimeConverter | None,
) -> int:
    """Parse and insert this block's transactions that are in ``needed``.

    ``block_index`` is the tx's position in the FULL block (the parser records it),
    so the filter to target hashes must not renumber; it enumerates every tx and
    skips the non-targets. Updates ``seen`` and returns the count inserted."""
    transactions = block.get("transactions", [])
    block_id = block.get("id", "")
    block_height = block.get("height", 0)
    block_time = converter.slot_to_utc(slot) if converter else None

    normalized: list[NormalizedTransaction] = []
    for block_index, tx_data in enumerate(transactions):
        tx_id = tx_data.get("id")
        if tx_id not in needed or tx_id in seen:
            continue
        tx = parse_ogmios_transaction(
            tx_data,
            block_slot=slot,
            block_hash=block_id,
            block_height=block_height,
            timestamp=block_time,
            block_index=block_index,
        )
        tx.network = network
        normalized.append(tx)
        seen.add(tx_id)

    if not normalized:
        return 0
    normalized = await resolve_input_amounts(normalized, network)
    await clickhouse.insert_transactions_batch_async(normalized)
    return len(normalized)
