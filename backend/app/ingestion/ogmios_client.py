"""Ogmios v6 WebSocket client: chain sync, block persistence, rollback.

Uses three separate WebSocket connections (Ogmios multiplexes one mini-protocol per connection):
- Connection 1: ChainSync — findIntersection + nextBlock loop (this class)
- Connection 2: LocalTxMonitor — owned by the composed MempoolMonitor
  (app.ingestion.mempool_monitor), constructed here with the shared
  protocol helpers injected
- Connection 3: LocalStateQuery (on-demand) — queryLedgerState/utxo for input resolution
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional, Set, List, Tuple

import websockets

from app.config import settings
from app.db import clickhouse, postgres, raw_store
from app.ingestion.chain_time import SlotTimeConverter
from app.ingestion.input_enrichment import (
    apply_resolved_inputs,
    resolve_input_amounts,
)
from app.ingestion.mempool_monitor import MempoolMonitor
from app.ingestion.ogmios_parser import parse_ogmios_transaction
from app.ingestion.resilience import CircuitBreaker, ExponentialBackoff, run_with_reconnect
from app.models.transaction import NormalizedTransaction

logger = logging.getLogger(__name__)

# Minimum spacing between era-summary refetches triggered by blocks beyond
# the converter's forecast horizon. A node syncing through an era boundary
# leaves the horizon behind for many consecutive blocks; one refetch per
# interval catches up without turning every block into two extra queries.
SLOT_TIME_REFETCH_MIN_SECONDS = 60


class BlockPersistError(Exception):
    """A block's ClickHouse persistence failed after all retries.

    Raised INSTEAD of advancing the sync checkpoint: the exception propagates
    to run_chain_sync's error handler, trips the chain circuit breaker, and
    the reconnect replays the block from the unadvanced checkpoint. Replay is
    safe because every fact table is ReplacingMergeTree (idempotent insert).
    The previous behaviour (log + continue to save_sync_point) permanently
    lost the block's transactions from the analytics warehouse.
    """


class IntersectionNotFoundError(Exception):
    """The saved sync checkpoint does not intersect the node's chain.

    Raised when a resume `findIntersection` returns an error (Ogmios
    IntersectionNotFound) instead of silently falling through to `nextBlock`,
    which after a failed intersection streams from origin, i.e. a full
    genesis re-sync (weeks of mainnet re-ingestion). This is an operator
    condition: a rebuilt/replaced node, a wrong OGMIOS_WS_URL, or a checkpoint
    stranded on a pruned fork. It propagates to the reconnect handler and trips
    the chain circuit breaker so /health/ready reports DOWN, rather than
    quietly replaying the chain from the beginning.
    """


class OgmiosClient:
    """Ogmios v6 WebSocket client with mempool monitoring and chain sync."""

    def __init__(self, on_lifecycle_event: Optional[Callable[[dict], Awaitable[None]]] = None):
        self.ws_url = settings.OGMIOS_WS_URL
        self.network = settings.CARDANO_NETWORK
        self.on_lifecycle_event = on_lifecycle_event

        # Connection state
        self._chain_ws = None
        self._running = True   # set once here; disconnect() sets it False to stop loops
        self._connected_chain = False

        # Strong references to in-flight delayed score-repurge tasks: a bare
        # asyncio.create_task result is only weakly referenced by the loop
        # and can be garbage-collected mid-flight, silently dropping the
        # repurge. Tasks discard themselves on completion.
        self._repurge_tasks: Set[asyncio.Task] = set()

        # LocalStateQuery connection for UTxO input resolution (on-demand)
        self._query_ws = None
        self._query_lock = asyncio.Lock()

        # Slot-to-UTC converter for chain-time block timestamps; fetched
        # once per chain-sync session, None until then (wall-clock fallback).
        # The refetch flag is set when a block's slot falls beyond the
        # converter's forecast horizon (e.g. the node crossed an era
        # boundary since the last fetch); the chain loop then refetches,
        # throttled by SLOT_TIME_REFETCH_MIN_SECONDS.
        self._slot_time: Optional[SlotTimeConverter] = None
        self._slot_time_refetch_needed = False
        self._slot_time_fetched_at: Optional[datetime] = None

        # Telemetry — used by /health and pipeline_state
        self._started_at: datetime = datetime.now(timezone.utc)
        self._last_msg_at: Optional[datetime] = None       # any Ogmios message
        self._last_block_at: Optional[datetime] = None     # last roll-forward
        self._last_processed_slot: Optional[int] = None    # slot of last confirmed block
        self._tip_slot: Optional[int] = None               # chain tip reported by Ogmios

        # Resilience for the chain-sync connection; the mempool monitor owns
        # its own breaker so chain and mempool failures stay isolated.
        self._backoff_chain = ExponentialBackoff(max_delay=settings.OGMIOS_RECONNECT_MAX_DELAY)
        self._circuit_breaker_chain = CircuitBreaker(
            failure_threshold=settings.OGMIOS_CIRCUIT_BREAKER_THRESHOLD,
            cooldown=settings.OGMIOS_CIRCUIT_BREAKER_COOLDOWN,
        )

        # The mempool side (LocalTxMonitor loop, pending-tx index, enrichment
        # cache, dedup set) lives in its own class; the protocol helpers and
        # the LocalStateQuery connection stay here, injected as bound methods.
        self.mempool = MempoolMonitor(
            network=self.network,
            emit=self._emit,
            query_utxo=self._query_utxo,
            connect_ws=self._connect_ws,
            send_recv=self._send_recv,
        )

        # JSON-RPC request counter
        self._rpc_id = 0

    # --- JSON-RPC helpers ---

    def _next_id(self) -> str:
        self._rpc_id += 1
        return str(self._rpc_id)

    def _jsonrpc(self, method: str, params: Optional[dict] = None) -> str:
        msg = {"jsonrpc": "2.0", "method": method, "id": self._next_id()}
        if params:
            msg["params"] = params
        return json.dumps(msg)

    async def _send_recv(self, ws, method: str, params: Optional[dict] = None) -> dict:
        """Send a JSON-RPC request and wait for the response.

        Large frames (a busy block of Plutus txs serialises to tens of MB;
        the socket allows 64 MB) are parsed on the default executor so the
        event loop — which also serves the API, WebSocket feed, and mempool
        monitor — is not blocked for the parse duration. Small frames parse
        inline: the thread handoff costs more than the parse below the
        threshold.
        """
        msg = self._jsonrpc(method, params)
        await ws.send(msg)
        raw = await ws.recv()
        self._last_msg_at = datetime.now(timezone.utc)
        if len(raw) > settings.OGMIOS_PARSE_EXECUTOR_THRESHOLD_BYTES:
            return await asyncio.to_thread(json.loads, raw)
        return json.loads(raw)

    # --- WebSocket connection with resilience ---

    async def _connect_ws(self, label: str) -> websockets.WebSocketClientProtocol:
        """Open a WebSocket connection to Ogmios with ping/pong keepalive."""
        ws = await websockets.connect(
            self.ws_url,
            ping_interval=settings.OGMIOS_HEARTBEAT_INTERVAL,
            ping_timeout=settings.OGMIOS_HEARTBEAT_TIMEOUT,
            max_size=settings.OGMIOS_WS_MAX_FRAME_BYTES,
        )
        logger.info(f"Ogmios [{label}]: connected to {self.ws_url}")
        return ws

    # --- LocalStateQuery: UTxO input resolution ---

    async def _ensure_query_ws(self):
        """Return the persistent LocalStateQuery WebSocket, reconnecting if needed."""
        if self._query_ws is None:
            self._query_ws = await self._connect_ws("query")
        return self._query_ws

    async def _query_utxo(self, output_refs: List[dict]) -> List[dict]:
        """Query the current UTxO set for specific output references.

        Returns a list of UTxO objects with address and value for each reference
        that is still unspent. References already consumed return nothing (empty
        list or missing from results).
        """
        if not output_refs:
            return []
        async with self._query_lock:
            try:
                ws = await self._ensure_query_ws()
                resp = await self._send_recv(
                    ws, "queryLedgerState/utxo",
                    {"outputReferences": output_refs},
                )
                if "error" in resp:
                    err = resp["error"]
                    logger.warning(
                        f"Ogmios [query]: UTxO query returned error: "
                        f"code={err.get('code')}, message={err.get('message')}"
                    )
                    return []
                return resp.get("result", [])
            except Exception as e:
                logger.warning(f"Ogmios [query]: UTxO query failed: {e}")
                # Drop the connection so the next call reconnects
                if self._query_ws:
                    try:
                        await self._query_ws.close()
                    except Exception:
                        pass
                self._query_ws = None
                return []

    # --- Lifecycle event emission ---

    async def _emit(self, event: dict):
        """Emit a lifecycle event to the callback."""
        if self.on_lifecycle_event:
            try:
                await self.on_lifecycle_event(event)
            except Exception as e:
                logger.error(f"Error in lifecycle event callback: {e}")

    # --- Chain Sync ---

    async def run_chain_sync(self):
        """Connect to Ogmios and run the ChainSync mini-protocol with resilience."""
        async def _connect():
            self._chain_ws = await self._connect_ws("chain")
            return self._chain_ws

        async def _close():
            if self._chain_ws:
                await self._chain_ws.close()
                self._chain_ws = None

        def _set_connected(value: bool) -> None:
            self._connected_chain = value

        await run_with_reconnect(
            name="chain",
            is_running=lambda: self._running,
            breaker=self._circuit_breaker_chain,
            backoff=self._backoff_chain,
            connect=_connect,
            run_session=self._chain_sync_loop,
            on_connected=_set_connected,
            close=_close,
            poll_seconds=settings.OGMIOS_CIRCUIT_OPEN_POLL_SECONDS,
            stable_reset_seconds=settings.OGMIOS_SESSION_STABLE_RESET_SECONDS,
        )

    async def _chain_sync_loop(self, ws):
        """ChainSync: findIntersection → nextBlock loop."""
        # Replay any score repurges persisted before a restart/disconnect
        # BEFORE normal block processing resumes: a repurge lost inside the
        # delay window would leave a stale tx_class_scores row permanently
        # blocking re-scoring of a re-confirmed tx.
        await self._replay_pending_score_repurges()

        # Chain-time source for transactions.timestamp; per-session so a
        # hard fork's new era summary is picked up on the next reconnect.
        await self._fetch_slot_time_converter(ws)

        sync_point = await postgres.get_sync_point(self.network)

        if sync_point:
            logger.info(f"Ogmios [chain]: resuming from slot {sync_point['slot']}")
            resp = await self._send_recv(ws, "findIntersection", {
                "points": [{"slot": sync_point["slot"], "id": sync_point["id"]}]
            })
            # Fail loudly if the checkpoint does not intersect the node's chain.
            # Ogmios returns a JSON-RPC error (IntersectionNotFound) here; if we
            # fell through to nextBlock the read pointer would sit at origin and
            # we would silently re-sync from genesis.
            if "error" in resp:
                raise IntersectionNotFoundError(
                    f"findIntersection failed for saved checkpoint "
                    f"slot={sync_point['slot']} id={sync_point['id']}: "
                    f"{resp['error']}. The node may have been rebuilt/replaced, "
                    f"OGMIOS_WS_URL may point at the wrong network, or the "
                    f"checkpoint may be stranded on a pruned fork. Refusing to "
                    f"re-sync from genesis; resolve and restart."
                )
        else:
            logger.info("Ogmios [chain]: first run, starting from current tip")
            resp = await self._send_recv(ws, "findIntersection", {"points": ["origin"]})
            tip = resp.get("result", {}).get("tip", {})
            if isinstance(tip, dict) and "slot" in tip:
                resp = await self._send_recv(ws, "findIntersection", {
                    "points": [{"slot": tip["slot"], "id": tip["id"]}]
                })
                logger.info(f"Ogmios [chain]: intersected at tip slot {tip['slot']}")

        while self._running:
            # A block beyond the converter's forecast horizon requested
            # fresh era summaries (the node crossed an era boundary or a
            # long-lived session outgrew the horizon); refetch, throttled.
            if self._slot_time_refetch_needed and self._slot_time_refetch_due():
                await self._fetch_slot_time_converter(ws)
            resp = await self._send_recv(ws, "nextBlock")
            result = resp.get("result", {})
            direction = result.get("direction")

            if direction == "forward":
                await self._handle_roll_forward(result)
            elif direction == "backward":
                await self._handle_roll_backward(result)

    async def _fetch_slot_time_converter(self, ws) -> None:
        """Fetch systemStart and era summaries to build the slot-to-UTC
        converter for block timestamps.

        Best-effort, but NEVER destructive: on failure a previously built
        converter is kept (era summaries only change at a hard fork, so a
        stale converter beats reverting a whole session's replay to wall
        clock: the exact skew the converter exists to prevent). Only when
        no converter has ever been built do block timestamps fall back to
        ingestion wall clock. A degraded timestamp must never block sync.
        """
        converter = None
        try:
            start_resp = await self._send_recv(ws, "queryNetwork/startTime")
            eras_resp = await self._send_recv(ws, "queryLedgerState/eraSummaries")
            converter = SlotTimeConverter.from_ogmios(
                start_resp.get("result"), eras_resp.get("result"),
            )
        except Exception as e:
            logger.warning(f"Ogmios [chain]: slot-time queries failed: {e}")
        self._slot_time_fetched_at = datetime.now(timezone.utc)
        self._slot_time_refetch_needed = False
        if converter is not None:
            self._slot_time = converter
        elif self._slot_time is not None:
            logger.warning(
                "Ogmios [chain]: slot-time refetch failed; keeping the "
                "previous era summaries (they only change at a hard fork)"
            )
        else:
            logger.warning(
                "Ogmios [chain]: no slot-time converter available; block "
                "timestamps fall back to ingestion wall clock (chain-time "
                "baselines will skew during catch-up replay)"
            )

    def _slot_time_refetch_due(self) -> bool:
        """Throttle horizon-triggered era-summary refetches."""
        if self._slot_time_fetched_at is None:
            return True
        age = datetime.now(timezone.utc) - self._slot_time_fetched_at
        return age.total_seconds() >= SLOT_TIME_REFETCH_MIN_SECONDS

    async def _handle_roll_forward(self, result: dict):
        """Process a new block (rollForward).

        Orchestration only; each step lives in a focused helper. Order is
        load-bearing: persistence (checkpoint-blocking) comes before the
        observability writes, and save_sync_point is last so a failure
        anywhere above replays the block after reconnect.
        """
        block = result.get("block", {})
        block_id = block.get("id", "")
        block_slot = block.get("slot")
        block_height = block.get("height", 0)
        transactions = block.get("transactions", [])

        # Byron epoch-boundary blocks (Ogmios type "ebb") carry no slot and
        # no transactions. Falling through with a slot-0 default would call
        # save_sync_point(slot=0), resetting the checkpoint to genesis and
        # forcing a full re-sync on restart. Skip them without
        # checkpointing; the next real block checkpoints past this point.
        if block_slot is None:
            if transactions:
                # Not a known block shape: every slotted-era block carries
                # a slot and EBBs are transaction-free. Refusing to guess a
                # slot must NOT be a quiet skip: the next block's
                # save_sync_point would advance past this one and its
                # transactions would be lost forever (recall-first).
                # Raising trips the chain breaker so /health reports DOWN
                # and the block genuinely replays from the unadvanced
                # checkpoint, like every other persistence failure.
                raise BlockPersistError(
                    f"Block {block_id} has {len(transactions)} transactions "
                    f"but no slot; refusing to ingest under an invented "
                    f"slot or checkpoint past it. Checkpoint NOT advanced, "
                    f"block will replay after reconnect."
                )
            logger.info(
                f"Ogmios [chain]: slotless boundary block {block_id} "
                f"skipped (Byron EBB)"
            )
            return

        # Update telemetry: tip comes from the nextBlock result envelope
        tip = result.get("tip", {})
        if isinstance(tip, dict) and "slot" in tip:
            self._tip_slot = tip["slot"]
        now_utc = datetime.now(timezone.utc)
        self._last_block_at = now_utc
        self._last_processed_slot = block_slot

        if not transactions:
            await postgres.save_sync_point(self.network, block_slot, block_id)
            return

        now = datetime.now(timezone.utc)
        normalized_txs, confirmed_records = await self._parse_block_txs(
            transactions, block_id, block_slot, block_height, now,
        )

        # Resolve input amounts from ClickHouse + intra-block outputs
        if normalized_txs:
            try:
                normalized_txs = await resolve_input_amounts(
                    normalized_txs, self.network
                )
            except Exception as e:
                logger.error(f"Input amount resolution failed (non-fatal): {e}")

        if normalized_txs:
            # Raw store FIRST: when RAW_DATA_MAX_BYTES caps the ClickHouse
            # copy, the blob must exist before the row becomes visible to
            # the engine's unanalyzed poll, or the raw-fallback path races
            # an in-flight write. Both steps are checkpoint-blocking and
            # idempotent on replay (write-once files, RMT upsert), so the
            # ordering is crash-safe in either direction.
            await self._write_raw_payloads(normalized_txs, block_slot)

            # Batch persist to ClickHouse. Checkpoint-BLOCKING: retried with
            # backoff, then raises BlockPersistError so save_sync_point below
            # is never reached and the block replays after reconnect.
            await self._insert_block_with_retry(normalized_txs, block_slot)

            # Batch upsert lifecycle CONFIRMED (single DB round-trip for whole block)
            try:
                await postgres.batch_upsert_lifecycle_confirmed(confirmed_records)
            except Exception as e:
                logger.error(f"Error updating lifecycle for block {block_slot}: {e}")

            await self.mempool.record_displacements(normalized_txs, now_utc)
            await self.mempool.settle_confirmed(
                normalized_txs, now, block_id, block_slot, block_height,
            )

        await postgres.save_sync_point(self.network, block_slot, block_id)

        # Checkpoint advanced: the block can no longer replay, so the mempool
        # enrichment is consumed only now. Popping before save_sync_point
        # succeeds would let a transient Postgres failure replay the block
        # WITHOUT enrichment, and the replay's fresh ingestion_timestamp
        # would make the un-enriched copy permanently win the
        # ReplacingMergeTree merge (lost input addresses / total_input_value).
        for tx in normalized_txs:
            self.mempool.consume_enrichment(tx.tx_hash)

        logger.info(
            f"Block {block_height} (slot {block_slot}): "
            f"{len(normalized_txs)} transactions confirmed"
        )

    async def _parse_block_txs(
        self,
        transactions: List[dict],
        block_id: str,
        block_slot: int,
        block_height: int,
        now: datetime,
    ) -> Tuple[List[NormalizedTransaction], List[tuple]]:
        """Parse a block's raw txs, applying any cached mempool input
        resolution. Returns (normalized txs, lifecycle CONFIRMED records);
        a tx that fails to parse is logged and skipped, never fatal."""
        normalized_txs: List[NormalizedTransaction] = []
        confirmed_records: List[tuple] = []
        # transactions.timestamp is CHAIN time: the baselines window on it
        # (see baselines.py). At tip the slot-derived time agrees with wall
        # clock within seconds; during catch-up replay it lands history at
        # its true position instead of collapsing it into "now". Wall clock
        # only when the converter is unavailable. Lifecycle records below
        # keep `now`: confirmed_at is operational observation time.
        block_time = (
            self._slot_time.slot_to_utc(block_slot) if self._slot_time else None
        )
        if block_time is None and self._slot_time is not None:
            # The slot is beyond the summaries' forecast horizon: the node
            # crossed an era boundary since the fetch, or a long-lived
            # session outgrew the horizon. Extrapolating could be days
            # wrong, so this block gets wall clock and the chain loop is
            # asked to refetch fresh summaries.
            if not self._slot_time_refetch_needed:
                logger.warning(
                    f"Ogmios [chain]: slot {block_slot} is beyond the era "
                    f"summaries' forecast horizon; using wall-clock "
                    f"timestamps until the summaries refresh"
                )
            self._slot_time_refetch_needed = True
        tx_timestamp = block_time or now
        for block_index, tx_data in enumerate(transactions):
            try:
                tx = parse_ogmios_transaction(
                    tx_data,
                    block_slot=block_slot,
                    block_hash=block_id,
                    block_height=block_height,
                    timestamp=tx_timestamp,
                    block_index=block_index,
                )
                tx.network = self.network

                # Apply cached input resolution from mempool observation.
                # Read-only here: the entry is popped only after the sync
                # checkpoint advances, so any replay (failed insert OR failed
                # checkpoint write) re-parses WITH the enrichment.
                cached = self.mempool.peek_enrichment(tx.tx_hash)
                if cached:
                    tx = apply_resolved_inputs(tx, cached[0])

                normalized_txs.append(tx)
                confirmed_records.append(
                    (tx.tx_hash, self.network, now, block_id, block_slot, block_height)
                )
            except Exception as e:
                tx_id = tx_data.get("id", "unknown")
                logger.error(f"Error parsing transaction {tx_id}: {e}")
                # The tx really did confirm on-chain — only OUR parser
                # choked — so without this it was previously dropped from
                # every store, including the data lake, and could never be
                # replayed once the parser bug was fixed (review finding).
                # Best-effort: a raw-store failure must not also lose the
                # block itself.
                if settings.RAW_STORE_ENABLED:
                    try:
                        await raw_store.write_parse_failed(
                            self.network, tx_id, tx_data, now,
                        )
                    except Exception as store_e:
                        logger.error(
                            f"Failed to preserve raw payload for unparseable "
                            f"tx {tx_id}: {store_e}"
                        )
        return normalized_txs, confirmed_records

    async def _write_raw_payloads(
        self,
        normalized_txs: List[NormalizedTransaction],
        block_slot: int,
    ) -> None:
        """Write full raw payloads to the local filesystem store.

        Checkpoint-BLOCKING when RAW_DATA_MAX_BYTES > 0: ClickHouse then
        holds an empty-with-flag payload for oversized txs and the raw
        store is the ONLY full copy, so a swallowed write failure would
        silently destroy the engine's fallback for exactly the large
        attack-shaped txs it protects (review finding). Uncapped, the
        ClickHouse copy is complete and a failure only costs redundancy.
        Replay-safe: the write-once guard in _write_sync skips
        already-written files.

        Keyed by each tx's OWN row timestamp (chain time), never wall
        clock: raw_store.read_confirmed derives the blob's day directory
        from the ClickHouse row's timestamp, so the two must match or the
        engine's raw fallback misses the blob for any block replayed more
        than a day after its chain time (review finding). Using the row
        timestamp also makes the write-once path deterministic across
        replays that straddle midnight."""
        if not settings.RAW_STORE_ENABLED:
            return
        try:
            await asyncio.gather(*[
                raw_store.write_confirmed(
                    self.network, tx.tx_hash, tx.raw_data, tx.timestamp,
                )
                for tx in normalized_txs
                if tx.raw_data
            ])
        except Exception as e:
            if settings.RAW_DATA_MAX_BYTES > 0:
                raise BlockPersistError(
                    f"Block at slot {block_slot}: raw-store write failed and "
                    f"RAW_DATA_MAX_BYTES > 0 makes the raw store load-bearing; "
                    f"checkpoint NOT advanced, block will replay."
                ) from e
            logger.error(f"Error writing raw store for block {block_slot}: {e}")

    async def _insert_block_with_retry(
        self, normalized_txs: List[NormalizedTransaction], block_slot: int,
    ) -> None:
        """Persist a block's transactions to ClickHouse, retrying with
        exponential backoff; raise BlockPersistError on exhaustion.

        See BlockPersistError for why this must never be swallowed: a caught
        insert failure followed by save_sync_point loses the block forever.
        """
        delay = settings.CLICKHOUSE_INSERT_RETRY_BASE_DELAY_SECONDS
        max_attempts = settings.CLICKHOUSE_INSERT_MAX_RETRIES
        last_err: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                await clickhouse.insert_transactions_batch_async(normalized_txs)
                return
            except Exception as e:
                last_err = e
                logger.warning(
                    "ClickHouse insert for block %s failed "
                    "(attempt %d/%d): %s",
                    block_slot, attempt, max_attempts, e,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                    delay = min(
                        delay * 2,
                        settings.CLICKHOUSE_INSERT_RETRY_MAX_DELAY_SECONDS,
                    )
        raise BlockPersistError(
            f"Block at slot {block_slot}: ClickHouse insert failed after "
            f"{max_attempts} attempts; checkpoint NOT advanced, block will replay."
        ) from last_err

    async def _handle_roll_backward(self, result: dict):
        """Process a chain rollback (rollBackward)."""
        point = result.get("point", {})
        # Ogmios v6 returns the literal string "origin" when rolling back past
        # the volatile chain instead of a {slot, id} object.
        rolled_back_to_origin = isinstance(point, str)
        if rolled_back_to_origin:
            rollback_slot = 0
            rollback_id = ""
        else:
            rollback_slot = point.get("slot", 0)
            rollback_id = point.get("id", "")

        tip = result.get("tip", {})
        if isinstance(tip, dict) and "slot" in tip:
            self._tip_slot = tip["slot"]

        if rolled_back_to_origin:
            logger.warning(
                "Ogmios [chain]: rollback to origin (saved sync point past volatile chain)"
            )
        else:
            logger.warning(f"Ogmios [chain]: rollback to slot {rollback_slot}")

        # Skip on rollback-to-origin, mirroring the warehouse guard below.
        # rollback_slot is 0 there, so `WHERE slot > 0` would flip the ENTIRE
        # network's CONFIRMED history to ROLLED_BACK over what is a node-resync
        # artifact (checkpoint past the volatile chain), not a real reorg.
        if not rolled_back_to_origin:
            try:
                await postgres.mark_lifecycle_rolled_back(rollback_slot, self.network)
            except Exception as e:
                logger.error(f"Error marking rollback in PostgreSQL: {e}")

        # Purge orphaned-fork rows from the analytics warehouse so they
        # cannot feed scorers, baselines, or API reads. Deliberately NOT
        # wrapped in try/except: a failure here propagates, the connection
        # resets, and the node re-sends the rollback (the cleanup is
        # idempotent). Skipped on rollback-to-origin: that is a node-resync
        # artifact (checkpoint past the volatile chain), and wiping the
        # whole network's history on it would destroy the warehouse over a
        # transient condition.
        if settings.ROLLBACK_CLEANUP_ENABLED and not rolled_back_to_origin:
            purged_hashes = await clickhouse.delete_rolled_back_txs_async(
                self.network, rollback_slot,
            )
            if purged_hashes:
                logger.warning(
                    "Rollback cleanup: purged %d orphaned tx(s) past slot %s "
                    "from ClickHouse", len(purged_hashes), rollback_slot,
                )
                # Second tx_class_scores pass after a delay: an engine batch
                # in flight during the purge can insert a stale score row
                # right after it, which would block re-scoring forever via
                # the unanalyzed anti-join. Deleting a FRESH score of a
                # re-confirmed tx is recall-safe (it just re-scores).
                #
                # Persist the hashes BEFORE scheduling the in-memory task:
                # the task is volatile (restart, shutdown inside the delay
                # window, task failure) and the persisted row is what
                # guarantees the repurge is replayed on the next chain-sync
                # (re)connect. Deliberately NOT wrapped in try/except, like
                # the purge above: a write failure propagates, the
                # connection resets, and the node re-sends the rollback.
                await postgres.add_pending_score_repurges(
                    self.network, purged_hashes,
                )
                task = asyncio.create_task(
                    self._delayed_score_repurge(purged_hashes)
                )
                # Strong reference until completion: the event loop only
                # holds tasks weakly, and a GC'd task silently drops the
                # delayed pass (the persisted row would still recover it on
                # the next reconnect, but only after an unbounded delay).
                self._repurge_tasks.add(task)
                task.add_done_callback(self._repurge_tasks.discard)

                # Purge the optional clustering sidecar's verdicts for the same
                # orphaned txs (no-op when the module is off): drops ghost
                # contract_anomaly rows for vanished txs and makes any
                # re-confirmed tx "unclassified" again so the feed re-scores it.
                await clickhouse.delete_clustering_rows_async(
                    self.network, purged_hashes,
                )

        if rollback_id:
            await postgres.save_sync_point(self.network, rollback_slot, rollback_id)

        # Rolled-back txs may re-enter the mempool (valid in Cardano); clear
        # the monitor's dedup set so they re-enter tracking.
        self.mempool.clear_on_rollback()

        await self._emit({
            "eventType": "TX_ROLLED_BACK",
            "network": self.network,
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "rollbackPoint": {"slot": rollback_slot, "id": rollback_id},
        })

    async def _delayed_score_repurge(self, hashes: List[str]) -> None:
        """Re-delete tx_class_scores rows for rolled-back txs after a delay.

        Durable, not best-effort: the hashes were persisted to
        pending_score_repurges BEFORE this task was scheduled, and the row
        is cleared only AFTER the ClickHouse delete succeeds. If this task
        never completes (failure, shutdown inside the delay window), the
        persisted row replays the repurge on the next chain-sync
        (re)connect via _replay_pending_score_repurges, so a stale score
        row cannot permanently block re-scoring of a re-confirmed tx
        (missed-attack risk). Failures log instead of crashing chain sync
        precisely because the persisted row guarantees the retry.
        """
        await asyncio.sleep(settings.ROLLBACK_SCORE_REPURGE_DELAY_SECONDS)
        if not self._running:
            # Shutting down inside the delay window: nothing is lost. The
            # persisted pending_score_repurges row replays this repurge on
            # the next startup's chain-sync connect.
            return
        try:
            await clickhouse.delete_score_rows_async(self.network, hashes)
            await postgres.clear_pending_score_repurges(self.network, hashes)
            logger.info(
                "Rollback score repurge: re-cleared %d tx(s)", len(hashes),
            )
        except Exception:
            logger.exception(
                "Rollback score repurge failed; the persisted pending row "
                "will replay it on the next chain-sync (re)connect"
            )

    async def _replay_pending_score_repurges(self) -> None:
        """Execute score repurges persisted before a restart or disconnect.

        Runs on every chain-sync (re)connect, before block processing
        resumes: it covers the window where _delayed_score_repurge was
        scheduled but never completed (process restart, shutdown during
        the delay, ClickHouse outage). Rows are cleared only after the
        delete succeeds; on failure they stay queued for the next
        (re)connect rather than crashing the sync loop, since the chain
        reconnect path is the retry mechanism.
        """
        try:
            pending = await postgres.get_pending_score_repurges(self.network)
            if not pending:
                return
            await clickhouse.delete_score_rows_async(self.network, pending)
            await postgres.clear_pending_score_repurges(self.network, pending)
            logger.info(
                "Replayed %d pending score repurge(s) from before restart",
                len(pending),
            )
        except Exception:
            logger.exception(
                "Pending score repurge replay failed; rows stay queued for "
                "the next chain-sync (re)connect"
            )

    # --- Control ---

    async def disconnect(self):
        """Gracefully disconnect all WebSocket connections."""
        self._running = False
        await self.mempool.stop()
        for ws, label in [
            (self._chain_ws, "chain"),
            (self._query_ws, "query"),
        ]:
            if ws:
                try:
                    await ws.close()
                    logger.info(f"Ogmios [{label}]: disconnected")
                except Exception:
                    pass
        self._chain_ws = None
        self._query_ws = None

    @property
    def is_connected(self) -> bool:
        return self._connected_chain or self.mempool.connected

    @property
    def pipeline_state(self) -> str:
        """Derived health state for the chain-sync pipeline, using the PIPELINE_*
        thresholds in config:

        OK:       circuit breaker closed and a block seen within
                  PIPELINE_BLOCK_AGE_DEGRADED_SECONDS
        DEGRADED: circuit breaker half-open, or block age between the DEGRADED and
                  DOWN thresholds
        DOWN:     circuit breaker open, block age past PIPELINE_BLOCK_AGE_DOWN_SECONDS,
                  or never connected after PIPELINE_STARTUP_GRACE_SECONDS
        """
        cb = self._circuit_breaker_chain.state.value
        if cb == "OPEN":
            return "DOWN"

        if self._last_block_at is None:
            uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
            return "OK" if uptime < settings.PIPELINE_STARTUP_GRACE_SECONDS else "DEGRADED"

        block_age = (datetime.now(timezone.utc) - self._last_block_at).total_seconds()
        if block_age > settings.PIPELINE_BLOCK_AGE_DOWN_SECONDS:
            return "DOWN"
        if block_age > settings.PIPELINE_BLOCK_AGE_DEGRADED_SECONDS or cb == "HALF_OPEN":
            return "DEGRADED"
        return "OK"

    @property
    def status(self) -> dict:
        sync_lag = (
            max(0, self._tip_slot - self._last_processed_slot)
            if self._tip_slot is not None and self._last_processed_slot is not None
            else None
        )
        return {
            "pipeline_state": self.pipeline_state,
            "chain_sync": "connected" if self._connected_chain else "disconnected",
            "mempool_monitor": "connected" if self.mempool.connected else "disconnected",
            "circuit_breaker_chain": self._circuit_breaker_chain.state.value,
            "circuit_breaker_mempool": self.mempool.circuit_state,
            "last_processed_slot": self._last_processed_slot,
            "last_ogmios_msg_at": self._last_msg_at.isoformat() if self._last_msg_at else None,
            "sync_lag_slots": sync_lag,
            # 1 slot ≈ 1 s on Cardano (mainnet, preprod, and preview all use 1s slot length)
            "sync_lag_seconds": sync_lag,
            "ws_url": self.ws_url,
        }
