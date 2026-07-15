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
     + ``insert_transactions_batch_async``, and writes the full payload to the raw
     store exactly as live ingestion does. The rows written are byte-for-byte the
     same shape as live-synced ones (real fee, size, redeemers, chain-time, and
     the raw-store blob the engine falls back to for oversized txs), so backfilled
     transactions look identical to the detectors.

This session is deliberately isolated from the live chain-sync loop: it opens its
own WebSocket and NEVER calls ``save_sync_point``, so it cannot move the live
ingestion checkpoint. Inserts are idempotent (ReplacingMergeTree), so a re-run or
a mid-run reconnect is harmless.

Completeness is best-effort and reported, not enforced: when the run cannot
guarantee it saw every transaction (Kupo not yet synced past the target range, no
pre-earliest checkpoint to anchor, or chain-time unavailable), it still finishes
and surfaces ``complete=False`` with a ``degraded_reason`` for the operator to
judge, rather than failing the job.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import websockets

from app.config import settings
from app.db import clickhouse, raw_store
from app.ingestion import ogmios_rpc
from app.ingestion.chain_time import SlotTimeConverter
from app.ingestion.input_enrichment import resolve_input_amounts
from app.ingestion.kupo_client import KupoClient, KupoError
from app.ingestion.ogmios_parser import parse_ogmios_transaction
from app.models.transaction import NormalizedTransaction

logger = logging.getLogger(__name__)

# Chars of the address echoed in progress messages: enough to recognise it in a
# log without dumping the full ~100-char bech32 (mirrors the sidecar's
# _TARGET_PREVIEW so the two subsystems truncate identically).
_ADDR_PREVIEW = 24

# The slot-time query (system start + era summaries) is transient-failure prone
# on a busy Ogmios; retry a few times before falling back to a degraded run so a
# momentary hiccup doesn't cost every backfilled tx its chain-time.
_CHAIN_TIME_QUERY_ATTEMPTS = 3

# One in-scan reconnect: a single dropped WebSocket during a long walk should not
# fail the whole job. `seen` makes the re-covered prefix a no-op (idempotent), so
# reconnecting and re-intersecting resumes without duplicating work. More than one
# retry here would mask a genuinely unhealthy Ogmios; the job's overall timeout
# (BACKFILL_TIMEOUT_SECONDS, enforced by the caller) is the outer backstop.
_SCAN_RECONNECT_ATTEMPTS = 1


def _noop(_: str) -> None:  # pragma: no cover - default progress sink
    pass


class BackfillError(RuntimeError):
    """A backfill run failed (Ogmios intersection/parse/insert)."""


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
    # False when the run could not guarantee it captured every transaction; the
    # reason is human-readable and non-fatal (see the module docstring).
    complete: bool = True
    degraded_reason: str | None = None


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
        return await ogmios_rpc.send_recv(self._ws, method, params, request_id=str(self._id))

    async def slot_time_converter(self) -> SlotTimeConverter | None:
        """Build the slot→UTC converter so backfilled ``timestamp`` is chain-time,
        matching live ingestion. Retries a few times, then returns None (the run
        continues in a degraded mode, flagged by the caller, rather than failing)."""
        last_exc: Exception | None = None
        for _attempt in range(_CHAIN_TIME_QUERY_ATTEMPTS):
            try:
                start = await self._send_recv("queryNetwork/startTime")
                eras = await self._send_recv("queryLedgerState/eraSummaries")
                return SlotTimeConverter.from_ogmios(start.get("result"), eras.get("result"))
            except Exception as exc:  # pragma: no cover - defensive; query is non-critical
                last_exc = exc
        logger.warning(
            "Backfill: slot-time query failed after %d attempts (%s); chain-time unavailable",
            _CHAIN_TIME_QUERY_ATTEMPTS,
            last_exc,
        )
        return None

    async def find_intersection(self, points: list[dict | str]) -> None:
        resp = await self._send_recv("findIntersection", {"points": points})
        if "error" in resp:
            raise BackfillError(f"findIntersection failed: {resp['error']}")

    async def next_block(self) -> tuple[str, Any]:
        """``(direction, payload)`` for one ``nextBlock``: ``("forward", block)`` or
        ``("backward", point)``. Ogmios long-polls at the tip, but the backfill
        stops once it passes the last target slot, so it never blocks there.

        A JSON-RPC error response raises ``BackfillError`` rather than being
        treated as an empty result: without this, a persistent nextBlock error
        would spin the scan loop forever (it never advances ``seen``)."""
        resp = await self._send_recv("nextBlock")
        if "error" in resp:
            raise BackfillError(f"nextBlock failed: {resp['error']}")
        result = resp.get("result", {})
        return result.get("direction", ""), (
            result.get("block") if result.get("direction") == "forward" else result.get("point")
        )


async def _kupo_completeness_reason(kupo: KupoClient, latest_slot: int) -> str | None:
    """A ``degraded_reason`` when Kupo cannot vouch for a complete answer, else
    None. Best-effort: a failed health probe degrades (flags) the run, it does not
    fail it. Kupo only answers for the range it has indexed, so if its most-recent
    checkpoint is behind the newest target slot, or it is not connected to the
    node, the ``/matches`` result may be missing transactions."""
    try:
        health = await kupo.health()
    except KupoError as exc:
        logger.warning("Backfill: Kupo health probe failed (%s); completeness unknown", exc)
        return "kupo health unknown"
    status = health.get("connection_status")
    if status != "connected":
        return f"Kupo connection_status={status!r} (not connected)"
    checkpoint = health.get("most_recent_checkpoint")
    if isinstance(checkpoint, int) and checkpoint < latest_slot:
        return (
            f"Kupo indexed only to slot {checkpoint}, before the newest target "
            f"slot {latest_slot}; more recent matches may be missing"
        )
    return None


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
        progress(f"Kupo has no matches for {address[:_ADDR_PREVIEW]}…; nothing to backfill")
        return BackfillResult(address, 0, 0, 0, [])

    needed = {p.tx_hash for p in points}
    earliest = min(p.slot for p in points)
    latest = max(p.slot for p in points)
    progress(
        f"backfilling {len(needed)} txs for {address[:_ADDR_PREVIEW]}… "
        f"(slots {earliest} to {latest})"
    )

    # Collect completeness caveats as we go; a non-empty list means complete=False.
    degraded: list[str] = []
    kupo_reason = await _kupo_completeness_reason(kupo, latest)
    if kupo_reason is not None:
        degraded.append(kupo_reason)
        progress(f"completeness caveat: {kupo_reason}")

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
        degraded.append("no pre-earliest checkpoint; the single earliest block may be skipped")
        progress("no pre-earliest checkpoint; the single earliest block may be skipped")

    seen: set[str] = set()
    blocks_scanned = 0
    txs_ingested = 0
    converter: SlotTimeConverter | None = None
    converter_ready = False

    reconnects = 0
    while True:
        try:
            async with websockets.connect(
                settings.OGMIOS_WS_URL,
                ping_interval=settings.OGMIOS_HEARTBEAT_INTERVAL,
                ping_timeout=settings.OGMIOS_HEARTBEAT_TIMEOUT,
                max_size=settings.OGMIOS_WS_MAX_FRAME_BYTES,
            ) as ws:
                reader = _OgmiosReader(ws)
                # Query chain-time once; a reconnect reuses the converter rather
                # than re-running the (retrying) query.
                if not converter_ready:
                    converter = await reader.slot_time_converter()
                    converter_ready = True
                await reader.find_intersection([intersection])

                # The walk terminates: target blocks all have slot <= latest and
                # the chain advances strictly, so a block with slot > latest is
                # reached in finitely many steps even if some target hash never
                # appears (rolled back).
                while needed - seen:
                    direction, payload = await reader.next_block()
                    if direction == "backward":
                        # Historical/immutable region; a rollback only rewinds the
                        # read head. Any target already in `seen` stays ingested
                        # (idempotent), so keep reading forward.
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
            break
        except websockets.ConnectionClosed as exc:
            reconnects += 1
            if reconnects > _SCAN_RECONNECT_ATTEMPTS:
                raise BackfillError(
                    f"Ogmios connection dropped and did not recover after "
                    f"{_SCAN_RECONNECT_ATTEMPTS} reconnect(s)"
                ) from exc
            progress("Ogmios connection dropped; reconnecting once to resume the scan")

    if converter is None:
        degraded.append("chain-time unavailable; timestamps fell back to wall clock")

    missing = sorted(needed - seen)
    complete = not degraded
    degraded_reason = "; ".join(degraded) if degraded else None
    progress(
        f"backfill done: {txs_ingested} txs ingested from {blocks_scanned} blocks; "
        f"{len(missing)} target(s) not found"
        + ("" if complete else f"; degraded: {degraded_reason}")
    )
    return BackfillResult(
        address,
        len(needed),
        blocks_scanned,
        txs_ingested,
        missing,
        complete=complete,
        degraded_reason=degraded_reason,
    )


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
    skips the non-targets. Updates ``seen`` and returns the count inserted.

    Each target is parsed under its own guard: a single unparseable tx (a parser
    bug or an era-edge payload) is logged and its raw payload preserved for later
    replay, exactly as the live path does, instead of aborting the whole backfill
    and stranding every later target."""
    transactions = block.get("transactions", [])
    block_id = block.get("id", "")
    block_height = block.get("height", 0)
    block_time = converter.slot_to_utc(slot) if converter else None

    normalized: list[NormalizedTransaction] = []
    for block_index, tx_data in enumerate(transactions):
        tx_id = tx_data.get("id")
        if tx_id not in needed or tx_id in seen:
            continue
        try:
            tx = parse_ogmios_transaction(
                tx_data,
                block_slot=slot,
                block_hash=block_id,
                block_height=block_height,
                timestamp=block_time,
                block_index=block_index,
            )
        except Exception as exc:
            logger.error("Backfill: failed to parse target tx %s in block %s: %s", tx_id, slot, exc)
            await _preserve_unparseable(network, tx_id, tx_data, block_time)
            # Not added to `seen`: it did not ingest, so it is reported in
            # missing_tx_hashes. The walk still terminates (its block is delivered
            # once, and the slot>latest guard breaks the loop regardless).
            continue
        tx.network = network
        normalized.append(tx)
        seen.add(tx_id)

    if not normalized:
        return 0
    normalized = await resolve_input_amounts(normalized, network)
    await _write_raw_payloads(normalized, network, slot)
    await _insert_with_retry(normalized, slot)
    return len(normalized)


async def _preserve_unparseable(
    network: str, tx_id: object, tx_data: dict, ts: datetime | None
) -> None:
    """Best-effort: write an unparseable target's raw payload to the raw store so
    the confirmed tx can be replayed once the parser is fixed (mirrors the live
    path). A raw-store failure here must not also lose the rest of the block."""
    if not settings.RAW_STORE_ENABLED or not isinstance(tx_id, str):
        return
    try:
        await raw_store.write_parse_failed(network, tx_id, tx_data, ts or datetime.now(UTC))
    except Exception as exc:
        logger.error("Backfill: failed to preserve raw payload for unparseable tx %s: %s", tx_id, exc)


async def _write_raw_payloads(
    normalized: list[NormalizedTransaction], network: str, block_slot: int
) -> None:
    """Write each tx's full raw payload to the raw store before the ClickHouse
    insert, keyed by the tx's OWN chain-time timestamp (so ``read_confirmed``
    derives the same day directory the ClickHouse row points at).

    Load-bearing when ``RAW_DATA_MAX_BYTES > 0``: the ClickHouse copy of an
    oversized tx is then stored empty-with-flag, so the raw store holds the ONLY
    full copy and a swallowed write failure would silently destroy the engine's
    raw-data fallback for exactly the large, attack-shaped historical txs this
    backfill exists to onboard. Uncapped, the ClickHouse copy is complete and a
    failure only costs redundancy. Mirrors ``OgmiosClient._write_raw_payloads``."""
    if not settings.RAW_STORE_ENABLED:
        return
    try:
        await asyncio.gather(
            *[
                raw_store.write_confirmed(network, tx.tx_hash, tx.raw_data, tx.timestamp)
                for tx in normalized
                if tx.raw_data
            ]
        )
    except Exception as exc:
        if settings.RAW_DATA_MAX_BYTES > 0:
            raise BackfillError(
                f"Block at slot {block_slot}: raw-store write failed and "
                f"RAW_DATA_MAX_BYTES > 0 makes the raw store the only full copy of "
                f"oversized txs; refusing to insert a row whose fallback is missing"
            ) from exc
        logger.error("Backfill: raw-store write failed for block %s: %s", block_slot, exc)


async def _insert_with_retry(normalized: list[NormalizedTransaction], block_slot: int) -> None:
    """Insert a block's targets into ClickHouse with exponential backoff, mirroring
    ``OgmiosClient._insert_block_with_retry``. A transient ClickHouse hiccup should
    not abort a long backfill; on exhaustion raise ``BackfillError`` (the job fails
    and an idempotent re-run recovers) rather than silently dropping the block."""
    delay = settings.CLICKHOUSE_INSERT_RETRY_BASE_DELAY_SECONDS
    max_attempts = settings.CLICKHOUSE_INSERT_MAX_RETRIES
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await clickhouse.insert_transactions_batch_async(normalized)
            return
        except Exception as exc:
            last_err = exc
            logger.warning(
                "Backfill: ClickHouse insert for block %s failed (attempt %d/%d): %s",
                block_slot,
                attempt,
                max_attempts,
                exc,
            )
            if attempt < max_attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 2, settings.CLICKHOUSE_INSERT_RETRY_MAX_DELAY_SECONDS)
    raise BackfillError(
        f"Block at slot {block_slot}: ClickHouse insert failed after {max_attempts} attempts"
    ) from last_err
