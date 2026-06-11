"""Ogmios v6 WebSocket client for mempool monitoring and chain sync.

Uses three separate WebSocket connections (Ogmios multiplexes one mini-protocol per connection):
- Connection 1: LocalTxMonitor (mempool) — acquireMempool + nextTransaction loop
- Connection 2: ChainSync — findIntersection + nextBlock loop
- Connection 3: LocalStateQuery (on-demand) — queryLedgerState/utxo for input resolution
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Optional, Set, Dict, Any, List, Tuple

import websockets

from app.analysis.features import extract_fee, extract_lovelace, extract_ttl, flatten_assets
from app.config import settings
from app.db import clickhouse, postgres, raw_store
from app.ingestion.ogmios_parser import parse_ogmios_transaction
from app.ingestion.resilience import ExponentialBackoff, CircuitBreaker
from app.models.transaction import NormalizedTransaction, TransactionInput

logger = logging.getLogger(__name__)


class BlockPersistError(Exception):
    """A block's ClickHouse persistence failed after all retries.

    Raised INSTEAD of advancing the sync checkpoint: the exception propagates
    to run_chain_sync's error handler, trips the chain circuit breaker, and
    the reconnect replays the block from the unadvanced checkpoint. Replay is
    safe because every fact table is ReplacingMergeTree (idempotent insert).
    The previous behaviour (log + continue to save_sync_point) permanently
    lost the block's transactions from the analytics warehouse.
    """


def _parse_resolved_utxo(utxo: Dict[str, Any]) -> tuple:
    """Parse one resolved UTxO from queryLedgerState/utxo into
    ``((tx_id, index), {address, amount, assets})``.

    ``extract_lovelace`` handles both the v5 top-level ``{"lovelace": N}``
    and the v6 nested ``{"ada": {"lovelace": N}}`` value shapes. The previous
    v5-only read returned 0 for every v6 UTxO and mis-filed the ``ada``
    sub-dict as a native asset, so every mempool-resolved input carried
    amount=0 and total_input_value stayed NULL.
    """
    utxo_tx = utxo.get("transaction", {})
    utxo_id = utxo_tx.get("id", "") if isinstance(utxo_tx, dict) else ""
    utxo_index = utxo.get("index", 0)
    val = utxo.get("value", {})
    assets = flatten_assets(val)
    return (utxo_id, utxo_index), {
        "address": utxo.get("address", ""),
        "amount": int(extract_lovelace(val)),
        "assets": assets if assets else None,
    }


class PendingTxIndex:
    """Input-ref index over mempool-pending transactions (front-running).

    Tracks, per pending tx, the entry tuple ``(input_refs, first_seen_at,
    fee, first_input_addr, ttl)`` plus an inverted index ``(input_tx_hash,
    input_index) -> {pending tx ids}``. Collision checks were O(block txs x
    pending entries) set intersections on the event loop; the inverted index
    makes them O(refs of the tx being checked). Both structures are
    maintained exclusively through :meth:`track` / :meth:`untrack` so they
    cannot drift.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, tuple] = {}
        self._ref_index: Dict[tuple, Set[str]] = {}

    def track(self, tx_id: str, entry: tuple) -> None:
        """Register a pending tx in both the entry map and the ref index."""
        self._entries[tx_id] = entry
        for ref in entry[0]:
            self._ref_index.setdefault(ref, set()).add(tx_id)

    def untrack(self, tx_id: str) -> None:
        """Remove a pending tx from the entry map and the ref index."""
        entry = self._entries.pop(tx_id, None)
        if entry is None:
            return
        for ref in entry[0]:
            ids = self._ref_index.get(ref)
            if ids is not None:
                ids.discard(tx_id)
                if not ids:
                    self._ref_index.pop(ref, None)

    def get(self, tx_id: str) -> Optional[tuple]:
        return self._entries.get(tx_id)

    def sharing(self, refs: Set[tuple]) -> Dict[str, Set[tuple]]:
        """Map pending tx id -> the subset of ``refs`` it also spends."""
        out: Dict[str, Set[tuple]] = {}
        for ref in refs:
            for pending_id in self._ref_index.get(ref, ()):
                out.setdefault(pending_id, set()).add(ref)
        return out

    def stale_ids(self, cutoff: datetime) -> List[str]:
        """Pending tx ids first seen before ``cutoff`` (for TTL pruning)."""
        return [k for k, v in self._entries.items() if v[1] < cutoff]


class OgmiosClient:
    """Ogmios v6 WebSocket client with mempool monitoring and chain sync."""

    def __init__(self, on_lifecycle_event: Optional[Callable[[dict], Awaitable[None]]] = None):
        self.ws_url = settings.OGMIOS_WS_URL
        self.network = settings.CARDANO_NETWORK
        self.on_lifecycle_event = on_lifecycle_event

        # Connection state
        self._chain_ws = None
        self._mempool_ws = None
        self._running = True   # set once here; disconnect() sets it False to stop loops
        self._connected_chain = False
        self._connected_mempool = False

        # Mempool deduplication (per-snapshot, cleared on reconnect and rollback)
        self._seen_mempool_txs: Set[str] = set()

        # LocalStateQuery connection for UTxO input resolution (on-demand)
        self._query_ws = None
        self._query_lock = asyncio.Lock()

        # Front-running collision detection: pending-tx input-ref index
        # (see PendingTxIndex for the entry shape and complexity rationale).
        self._pending = PendingTxIndex()

        # Cache of resolved UTxO inputs for PENDING transactions.
        # Populated by the mempool monitor via queryLedgerState/utxo, consumed
        # by _handle_roll_forward when the tx is confirmed in a block; popped
        # only AFTER the block durably persists, so a retry-exhausted insert
        # or rollback replay re-parses with the enrichment intact.
        # Key: tx_hash, Value: ({(input_tx_hash, input_index): {address,
        # amount, assets}}, cached_at) — the timestamp lets the prune sweep
        # evict entries whose tx never reached the pending index (e.g.
        # collision tracking threw before track()), which previously leaked
        # until restart.
        self._pending_input_cache: Dict[str, Tuple[Dict[tuple, dict], datetime]] = {}

        # Telemetry — used by /health and pipeline_state
        self._started_at: datetime = datetime.now(timezone.utc)
        self._last_msg_at: Optional[datetime] = None       # any Ogmios message
        self._last_block_at: Optional[datetime] = None     # last roll-forward
        self._last_processed_slot: Optional[int] = None    # slot of last confirmed block
        self._tip_slot: Optional[int] = None               # chain tip reported by Ogmios

        # Resilience — separate circuit breakers so chain and mempool failures
        # are isolated from each other
        self._backoff_chain = ExponentialBackoff(max_delay=settings.OGMIOS_RECONNECT_MAX_DELAY)
        self._backoff_mempool = ExponentialBackoff(max_delay=settings.OGMIOS_RECONNECT_MAX_DELAY)
        self._circuit_breaker_chain = CircuitBreaker(
            failure_threshold=settings.OGMIOS_CIRCUIT_BREAKER_THRESHOLD,
            cooldown=settings.OGMIOS_CIRCUIT_BREAKER_COOLDOWN,
        )
        self._circuit_breaker_mempool = CircuitBreaker(
            failure_threshold=settings.OGMIOS_CIRCUIT_BREAKER_THRESHOLD,
            cooldown=settings.OGMIOS_CIRCUIT_BREAKER_COOLDOWN,
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
            max_size=64 * 1024 * 1024,  # 64MB for large blocks
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

    async def _resolve_mempool_inputs(self, tx_id: str, tx_data: dict):
        """Resolve UTxO inputs for a PENDING transaction and cache the results.

        When a tx is in the mempool, its inputs are guaranteed unspent (the node
        validated this). We query Ogmios LocalStateQuery to get each input's
        address and lovelace value, then store the mapping in _pending_input_cache
        for use when ChainSync confirms the tx.
        """
        raw_inputs = tx_data.get("inputs", [])
        if not raw_inputs:
            return

        output_refs = []
        for inp in raw_inputs:
            tx_obj = inp.get("transaction", {})
            if isinstance(tx_obj, dict) and tx_obj.get("id"):
                output_refs.append({
                    "transaction": {"id": tx_obj["id"]},
                    "index": inp.get("index", 0),
                })

        if not output_refs:
            return

        utxos = await self._query_utxo(output_refs)
        if not utxos:
            return

        cache_entry: Dict[tuple, dict] = {}
        for utxo in utxos:
            ref, resolved = _parse_resolved_utxo(utxo)
            cache_entry[ref] = resolved

        if cache_entry:
            self._pending_input_cache[tx_id] = (
                cache_entry, datetime.now(timezone.utc),
            )
            logger.debug(
                f"Resolved {len(cache_entry)}/{len(output_refs)} inputs for pending tx {tx_id}"
            )

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
        while self._running:
            if not self._circuit_breaker_chain.can_attempt():
                logger.warning("Ogmios [chain]: circuit breaker OPEN, waiting for cooldown")
                await asyncio.sleep(10)
                continue

            try:
                self._chain_ws = await self._connect_ws("chain")
                self._connected_chain = True
                self._circuit_breaker_chain.record_success()
                self._backoff_chain.reset()

                await self._chain_sync_loop(self._chain_ws)

            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                logger.warning(f"Ogmios [chain]: connection lost: {e}")
                self._connected_chain = False
                self._circuit_breaker_chain.record_failure()
                await self._backoff_chain.wait()
            except Exception as e:
                logger.error(f"Ogmios [chain]: unexpected error: {e}")
                self._connected_chain = False
                self._circuit_breaker_chain.record_failure()
                await self._backoff_chain.wait()
            finally:
                if self._chain_ws:
                    await self._chain_ws.close()
                    self._chain_ws = None

    async def _chain_sync_loop(self, ws):
        """ChainSync: findIntersection → nextBlock loop."""
        sync_point = await postgres.get_sync_point(self.network)

        if sync_point:
            logger.info(f"Ogmios [chain]: resuming from slot {sync_point['slot']}")
            resp = await self._send_recv(ws, "findIntersection", {
                "points": [{"slot": sync_point["slot"], "id": sync_point["id"]}]
            })
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
            resp = await self._send_recv(ws, "nextBlock")
            result = resp.get("result", {})
            direction = result.get("direction")

            if direction == "forward":
                await self._handle_roll_forward(result)
            elif direction == "backward":
                await self._handle_roll_backward(result)

    @staticmethod
    def _apply_resolved_inputs(
        tx: NormalizedTransaction,
        resolved: Dict[tuple, dict],
    ) -> NormalizedTransaction:
        """Enrich a NormalizedTransaction with previously resolved UTxO input data.

        Attempted inputs of a failed tx ARE resolved (their addresses are
        attack-attempt signal and belong in the address screen) but never
        feed total_input_value: the ledger did not consume them.
        """
        total = 0
        new_inputs = []
        for inp in tx.inputs:
            if not inp.is_collateral and not inp.is_reference:
                utxo = resolved.get((inp.tx_hash, inp.index))
                if utxo:
                    inp = TransactionInput(
                        tx_hash=inp.tx_hash,
                        index=inp.index,
                        address=utxo["address"],
                        amount=utxo["amount"],
                        assets=utxo.get("assets"),
                        is_reference=False,
                        is_collateral=False,
                        is_unspent_attempt=inp.is_unspent_attempt,
                    )
                    if not inp.is_unspent_attempt:
                        total += utxo["amount"]
            new_inputs.append(inp)

        resolved_addrs = {
            i.address for i in new_inputs
            if i.address and not i.is_collateral and not i.is_reference
        }
        return tx.model_copy(update={
            "inputs": new_inputs,
            "total_input_value": total if total > 0 else None,
            "addresses": list(set(tx.addresses) | resolved_addrs),
        })

    async def _resolve_input_amounts(
        self, txs: List[NormalizedTransaction]
    ) -> List[NormalizedTransaction]:
        """Resolve input addresses and amounts from ClickHouse and intra-block outputs.

        1. Build an intra-block output map from earlier txs in this block.
        2. Collect all unresolved (input_tx_hash, input_index) refs.
        3. Batch-fetch from ClickHouse for cross-block refs.
        4. Apply resolved values to each input.
        """
        # Build intra-block output map: {(tx_hash, output_index): (address, amount)}.
        # Collateral returns included at their EXPLICIT on-chain index (the
        # regular-output count, Babbage): they are real spendable UTxOs and
        # a same-block spend of one must resolve.
        intra_block: Dict[tuple, tuple] = {}
        for tx in txs:
            for idx, out in enumerate(tx.outputs):
                chain_idx = out.output_index if out.output_index is not None else idx
                intra_block[(tx.tx_hash, chain_idx)] = (out.address, out.amount)

        # Collect all unresolved input refs (skip already-resolved from mempool cache)
        cross_block_refs = []
        for tx in txs:
            for inp in tx.inputs:
                if inp.is_collateral or inp.is_reference:
                    continue
                if inp.amount > 0:
                    continue  # already resolved
                ref = (inp.tx_hash, inp.index)
                if ref not in intra_block:
                    cross_block_refs.append(ref)

        # Batch fetch from ClickHouse
        ch_resolved: Dict[tuple, tuple] = {}
        if cross_block_refs:
            ch_resolved = await clickhouse.get_outputs_for_refs_async(
                cross_block_refs, self.network
            )

        # Merge: intra-block takes priority over ClickHouse
        all_resolved = {**ch_resolved, **intra_block}

        # Apply to each tx
        result = []
        for tx in txs:
            total = 0
            new_inputs = []
            changed = False
            for inp in tx.inputs:
                if inp.is_collateral or inp.is_reference:
                    new_inputs.append(inp)
                    continue  # don't include collateral/reference in total_input_value
                if inp.amount > 0:
                    if not inp.is_unspent_attempt:
                        total += inp.amount
                    new_inputs.append(inp)
                    continue
                ref = (inp.tx_hash, inp.index)
                resolved = all_resolved.get(ref)
                if resolved:
                    addr, amt = resolved
                    new_inputs.append(TransactionInput(
                        tx_hash=inp.tx_hash,
                        index=inp.index,
                        address=addr,
                        amount=int(amt),
                        assets=inp.assets,
                        is_reference=False,
                        is_collateral=False,
                        is_unspent_attempt=inp.is_unspent_attempt,
                    ))
                    # Attempted inputs of a failed tx resolve for address
                    # visibility but were never consumed: keep them out of
                    # total_input_value.
                    if not inp.is_unspent_attempt:
                        total += int(amt)
                    changed = True
                else:
                    new_inputs.append(inp)

            if changed:
                resolved_addrs = {
                    i.address for i in new_inputs
                    if i.address and not i.is_collateral and not i.is_reference
                }
                tx = tx.model_copy(update={
                    "inputs": new_inputs,
                    "total_input_value": total if total > 0 else None,
                    "addresses": list(set(tx.addresses) | resolved_addrs),
                })
            result.append(tx)
        return result

    async def _handle_roll_forward(self, result: dict):
        """Process a new block (rollForward).

        Orchestration only; each step lives in a focused helper. Order is
        load-bearing: persistence (checkpoint-blocking) comes before the
        observability writes, and save_sync_point is last so a failure
        anywhere above replays the block after reconnect.
        """
        block = result.get("block", {})
        block_id = block.get("id", "")
        block_slot = block.get("slot", 0)
        block_height = block.get("height", 0)
        transactions = block.get("transactions", [])

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
        normalized_txs, confirmed_records = self._parse_block_txs(
            transactions, block_id, block_slot, block_height, now,
        )

        # Resolve input amounts from ClickHouse + intra-block outputs
        if normalized_txs:
            try:
                normalized_txs = await self._resolve_input_amounts(normalized_txs)
            except Exception as e:
                logger.error(f"Input amount resolution failed (non-fatal): {e}")

        if normalized_txs:
            # Raw store FIRST: when RAW_DATA_MAX_BYTES caps the ClickHouse
            # copy, the blob must exist before the row becomes visible to
            # the engine's unanalyzed poll, or the raw-fallback path races
            # an in-flight write. Both steps are checkpoint-blocking and
            # idempotent on replay (write-once files, RMT upsert), so the
            # ordering is crash-safe in either direction.
            await self._write_raw_payloads(normalized_txs, block_slot, now)

            # Batch persist to ClickHouse. Checkpoint-BLOCKING: retried with
            # backoff, then raises BlockPersistError so save_sync_point below
            # is never reached and the block replays after reconnect.
            await self._insert_block_with_retry(normalized_txs, block_slot)

            # Block is durable: the mempool enrichment is consumed. A
            # BlockPersistError above leaves the cache intact for the replay.
            for tx in normalized_txs:
                self._pending_input_cache.pop(tx.tx_hash, None)

            # Batch upsert lifecycle CONFIRMED (single DB round-trip for whole block)
            try:
                await postgres.batch_upsert_lifecycle_confirmed(confirmed_records)
            except Exception as e:
                logger.error(f"Error updating lifecycle for block {block_slot}: {e}")

            await self._record_displacements(normalized_txs, now_utc)
            await self._settle_confirmed(
                normalized_txs, now, block_id, block_slot, block_height,
            )

        await postgres.save_sync_point(self.network, block_slot, block_id)

        logger.info(
            f"Block {block_height} (slot {block_slot}): "
            f"{len(normalized_txs)} transactions confirmed"
        )

    def _parse_block_txs(
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
        for block_index, tx_data in enumerate(transactions):
            try:
                tx = parse_ogmios_transaction(
                    tx_data,
                    block_slot=block_slot,
                    block_hash=block_id,
                    block_height=block_height,
                    timestamp=now,
                    block_index=block_index,
                )
                tx.network = self.network

                # Apply cached input resolution from mempool observation.
                # Read-only here: the entry is popped only after the block
                # persists, so a failed insert replays WITH the enrichment.
                cached = self._pending_input_cache.get(tx.tx_hash)
                if cached:
                    tx = self._apply_resolved_inputs(tx, cached[0])

                normalized_txs.append(tx)
                confirmed_records.append(
                    (tx.tx_hash, self.network, now, block_id, block_slot, block_height)
                )
            except Exception as e:
                tx_id = tx_data.get("id", "unknown")
                logger.error(f"Error parsing transaction {tx_id}: {e}")
        return normalized_txs, confirmed_records

    async def _write_raw_payloads(
        self,
        normalized_txs: List[NormalizedTransaction],
        block_slot: int,
        now: datetime,
    ) -> None:
        """Write full raw payloads to the local filesystem store.

        Checkpoint-BLOCKING when RAW_DATA_MAX_BYTES > 0: ClickHouse then
        holds an empty-with-flag payload for oversized txs and the raw
        store is the ONLY full copy, so a swallowed write failure would
        silently destroy the engine's fallback for exactly the large
        attack-shaped txs it protects (review finding). Uncapped, the
        ClickHouse copy is complete and a failure only costs redundancy.
        Replay-safe: the write-once guard in _write_sync skips
        already-written files."""
        if not settings.RAW_STORE_ENABLED:
            return
        try:
            await asyncio.gather(*[
                raw_store.write_confirmed(self.network, tx.tx_hash, tx.raw_data, now)
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

    @staticmethod
    def _consumed_refs(tx: NormalizedTransaction) -> Set[tuple]:
        """The input refs a confirmed tx actually CONSUMED: the regular
        inputs for a validated tx, the collaterals for a phase-2-failed one
        (the ledger spends collateral on failure; the regular inputs stay
        live)."""
        if tx.script_valid:
            return {
                (inp.tx_hash, inp.index) for inp in tx.inputs
                if not inp.is_collateral and not inp.is_reference
            }
        return {
            (inp.tx_hash, inp.index) for inp in tx.inputs
            if inp.is_collateral
        }

    async def _record_displacements(
        self,
        normalized_txs: List[NormalizedTransaction],
        now_utc: datetime,
    ) -> None:
        """Detect displacement: a confirmed tx may spend inputs that a
        still-pending tx also wanted. This is the primary Cardano
        front-running signal (two competing txs are never in the same
        node's mempool simultaneously)."""
        confirmed_hashes = {tx.tx_hash for tx in normalized_txs}
        for tx in normalized_txs:
            conf_refs = self._consumed_refs(tx)
            if not conf_refs:
                continue
            for pending_id, shared in self._pending.sharing(conf_refs).items():
                if pending_id in confirmed_hashes:
                    continue  # this pending tx is also in this block
                entry = self._pending.get(pending_id)
                if entry is None:
                    continue
                (_pending_refs, pending_seen, pending_fee,
                 pending_addr, pending_ttl) = entry
                if shared:
                    delta_ms = (now_utc - pending_seen).total_seconds() * 1000
                    conf_addr = ""
                    conf_inp = next(
                        (i for i in tx.inputs if not i.is_collateral and not i.is_reference and i.address),
                        None,
                    )
                    if conf_inp:
                        conf_addr = conf_inp.address
                    try:
                        await postgres.insert_mempool_collision(
                            tx_a=pending_id, tx_b=tx.tx_hash,
                            network=self.network,
                            shared_inputs=[list(s) for s in shared],
                            shared_count=len(shared),
                            tx_a_seen_at=pending_seen, tx_b_seen_at=now_utc,
                            delta_ms=delta_ms,
                            tx_a_fee=pending_fee, tx_b_fee=tx.fee or 0,
                            tx_a_first_input_addr=pending_addr,
                            tx_b_first_input_addr=conf_addr,
                            tx_a_ttl=pending_ttl, tx_b_ttl=0,
                        )
                        # Immediately mark outcome: the confirmed tx (tx_b) won
                        await postgres.update_collision_outcome(
                            tx.tx_hash, self.network
                        )
                        logger.info(
                            f"Displacement detected: pending {pending_id[:16]}.. "
                            f"displaced by confirmed {tx.tx_hash[:16]}.. "
                            f"({len(shared)} shared inputs)"
                        )
                    except Exception as e:
                        logger.debug(f"Displacement record error: {e}")

    async def _settle_confirmed(
        self,
        normalized_txs: List[NormalizedTransaction],
        now: datetime,
        block_id: str,
        block_slot: int,
        block_height: int,
    ) -> None:
        """Post-persistence settlement for confirmed txs: resolve collision
        outcomes, drop pending-tx tracking, broadcast TX_CONFIRMED (after DB
        writes so API queries are consistent), and clear the mempool dedup
        entries."""
        for tx in normalized_txs:
            self._pending.untrack(tx.tx_hash)
            try:
                await postgres.update_collision_outcome(
                    tx.tx_hash, self.network
                )
            except Exception:
                pass

        for tx in normalized_txs:
            await self._emit({
                "eventType": "TX_CONFIRMED",
                "txId": tx.tx_hash,
                "network": self.network,
                "observedAt": now.isoformat(),
                "block": {"hash": block_id, "slot": block_slot, "height": block_height},
            })

        for tx in normalized_txs:
            self._seen_mempool_txs.discard(tx.tx_hash)

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
            deleted = await clickhouse.delete_rolled_back_txs_async(
                self.network, rollback_slot,
            )
            if deleted:
                logger.warning(
                    "Rollback cleanup: purged %d orphaned tx(s) past slot %s "
                    "from ClickHouse", deleted, rollback_slot,
                )

        if rollback_id:
            await postgres.save_sync_point(self.network, rollback_slot, rollback_id)

        # Clear mempool dedup set so rolled-back txs can re-enter tracking
        # if they re-appear in the mempool (valid in Cardano)
        self._seen_mempool_txs.clear()

        await self._emit({
            "eventType": "TX_ROLLED_BACK",
            "network": self.network,
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "rollbackPoint": {"slot": rollback_slot, "id": rollback_id},
        })

    # --- Mempool Monitoring ---

    async def run_mempool_monitor(self):
        """Connect to Ogmios and run the LocalTxMonitor mini-protocol with resilience."""
        while self._running:
            if not self._circuit_breaker_mempool.can_attempt():
                await asyncio.sleep(10)
                continue

            try:
                self._mempool_ws = await self._connect_ws("mempool")
                self._connected_mempool = True
                self._circuit_breaker_mempool.record_success()
                self._backoff_mempool.reset()

                await self._mempool_loop(self._mempool_ws)

            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                logger.warning(f"Ogmios [mempool]: connection lost: {e}")
                self._connected_mempool = False
                self._circuit_breaker_mempool.record_failure()
                await self._backoff_mempool.wait()
            except Exception as e:
                logger.error(f"Ogmios [mempool]: unexpected error: {e}")
                self._connected_mempool = False
                self._circuit_breaker_mempool.record_failure()
                await self._backoff_mempool.wait()
            finally:
                if self._mempool_ws:
                    await self._mempool_ws.close()
                    self._mempool_ws = None

    async def _record_mempool_collisions(
        self, tx_id: str, tx_data: dict, now: datetime,
    ) -> None:
        """Record same-mempool input collisions for a newly seen pending tx,
        register it in the pending index, and run the throttled TTL prune.

        Same-mempool collisions are the rare path (a node rejects a second
        spend of the same UTxO); the common front-running signal is
        displacement, handled in _record_displacements on the chain side.
        """
        input_refs = set()
        tx_fee = extract_fee(tx_data)
        tx_ttl = extract_ttl(tx_data)
        # Extract first input address for address clustering.
        # Ogmios mempool inputs are unresolved references (tx_hash + index),
        # not full UTxOs with addresses. Extract the address field if present
        # (resolved inputs), otherwise leave empty.
        first_input_addr = ""
        first_inp = (tx_data.get("inputs") or [None])[0]
        if first_inp:
            first_input_addr = first_inp.get("address", "")
        for inp in tx_data.get("inputs", []):
            inp_tx = inp.get("transaction", {})
            inp_hash = inp_tx.get("id", "") if isinstance(inp_tx, dict) else str(inp_tx)
            input_refs.add((inp_hash, inp.get("index", 0)))

        if input_refs:
            # Check for collisions with existing pending txs
            # (ref-index lookup: O(refs), not O(pending entries))
            for other_id, shared in self._pending.sharing(input_refs).items():
                other_entry = self._pending.get(other_id)
                if other_entry is None:
                    continue
                (_other_refs, other_seen, other_fee,
                 other_addr, other_ttl) = other_entry
                if shared:
                    delta_ms = (now - other_seen).total_seconds() * 1000
                    try:
                        await postgres.insert_mempool_collision(
                            tx_a=other_id, tx_b=tx_id,
                            network=self.network,
                            shared_inputs=[list(s) for s in shared],
                            shared_count=len(shared),
                            tx_a_seen_at=other_seen, tx_b_seen_at=now,
                            delta_ms=delta_ms,
                            tx_a_fee=other_fee, tx_b_fee=tx_fee,
                            tx_a_first_input_addr=other_addr,
                            tx_b_first_input_addr=first_input_addr,
                            tx_a_ttl=other_ttl, tx_b_ttl=tx_ttl,
                        )
                        logger.info(
                            f"Mempool collision: {other_id[:16]}.. vs {tx_id[:16]}.. "
                            f"({len(shared)} shared inputs, delta={delta_ms:.0f}ms)"
                        )
                    except Exception as e:
                        logger.error(f"Failed to record collision: {e}")

            self._pending.track(
                tx_id, (input_refs, now, tx_fee, first_input_addr, tx_ttl),
            )

        # Prune stale entries (throttled; knobs documented in config.py)
        if len(self._seen_mempool_txs) % settings.MEMPOOL_PRUNE_EVERY_N_TXS == 0:
            cutoff = now - timedelta(seconds=settings.MEMPOOL_PENDING_TTL_SECONDS)
            for k in self._pending.stale_ids(cutoff):
                self._pending.untrack(k)
                self._pending_input_cache.pop(k, None)
            # Sweep cache entries by their own age too: an entry whose tx
            # never reached the pending index (collision tracking threw
            # before track()) is invisible to stale_ids and leaked forever.
            orphaned = [
                k for k, (_, cached_at) in self._pending_input_cache.items()
                if cached_at < cutoff
            ]
            for k in orphaned:
                self._pending_input_cache.pop(k, None)
            # Cap dedup set to prevent unbounded growth
            if len(self._seen_mempool_txs) > settings.MEMPOOL_SEEN_TXS_MAX:
                self._seen_mempool_txs.clear()

    async def _mempool_loop(self, ws):
        """LocalTxMonitor: acquireMempool → nextTransaction loop."""
        # Clear dedup set on (re)connect so the fresh snapshot is fully processed.
        # The DB's ON CONFLICT DO NOTHING handles any genuine duplicates safely.
        self._seen_mempool_txs.clear()

        while self._running:
            resp = await self._send_recv(ws, "acquireMempool")
            snapshot_slot = resp.get("result", {}).get("slot")
            logger.debug(f"Ogmios [mempool]: acquired snapshot at slot {snapshot_slot}")

            while self._running:
                resp = await self._send_recv(ws, "nextTransaction", {"fields": "all"})
                tx_data = resp.get("result", {}).get("transaction")

                if tx_data is None:
                    # Snapshot exhausted, re-acquire
                    break

                tx_id = tx_data.get("id", "")
                if not tx_id or tx_id in self._seen_mempool_txs:
                    continue

                self._seen_mempool_txs.add(tx_id)
                now = datetime.now(timezone.utc)

                # Collision detection: extract input refs and check against pending txs
                try:
                    await self._record_mempool_collisions(tx_id, tx_data, now)
                except Exception as e:
                    logger.debug(f"Collision detection error for {tx_id}: {e}")

                # Resolve UTxO inputs while the tx is still PENDING (inputs
                # are guaranteed unspent at this point). Results are cached
                # for use when ChainSync confirms the tx.
                try:
                    await self._resolve_mempool_inputs(tx_id, tx_data)
                except Exception as e:
                    logger.debug(f"Input resolution failed for {tx_id}: {e}")

                # Write raw mempool payload to local filesystem store (non-blocking)
                if settings.RAW_STORE_ENABLED:
                    asyncio.create_task(
                        raw_store.write_mempool(self.network, tx_id, tx_data, now)
                    )

                try:
                    await postgres.upsert_lifecycle_pending(
                        tx_id=tx_id,
                        network=self.network,
                        first_seen_at=now,
                    )
                except Exception as e:
                    logger.error(f"Error persisting pending tx {tx_id}: {e}")

                await self._emit({
                    "eventType": "TX_PENDING",
                    "txId": tx_id,
                    "network": self.network,
                    "observedAt": now.isoformat(),
                    "firstSeenAt": now.isoformat(),
                })

                logger.debug(f"TX_PENDING: {tx_id}")

    # --- Control ---

    async def disconnect(self):
        """Gracefully disconnect all WebSocket connections."""
        self._running = False
        for ws, label in [
            (self._chain_ws, "chain"),
            (self._mempool_ws, "mempool"),
            (self._query_ws, "query"),
        ]:
            if ws:
                try:
                    await ws.close()
                    logger.info(f"Ogmios [{label}]: disconnected")
                except Exception:
                    pass
        self._chain_ws = None
        self._mempool_ws = None
        self._query_ws = None

    @property
    def is_connected(self) -> bool:
        return self._connected_chain or self._connected_mempool

    @property
    def pipeline_state(self) -> str:
        """Derived health state for the chain-sync pipeline.

        OK       — circuit breaker closed, block received within last 120 s
        DEGRADED — circuit breaker half-open, or no block for 120-300 s
        DOWN     — circuit breaker open, or no block for > 300 s, or never
                   connected after a 60 s grace period
        """
        cb = self._circuit_breaker_chain.state.value
        if cb == "OPEN":
            return "DOWN"

        if self._last_block_at is None:
            uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
            return "OK" if uptime < 60 else "DEGRADED"

        block_age = (datetime.now(timezone.utc) - self._last_block_at).total_seconds()
        if block_age > 300:
            return "DOWN"
        if block_age > 120 or cb == "HALF_OPEN":
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
            "mempool_monitor": "connected" if self._connected_mempool else "disconnected",
            "circuit_breaker_chain": self._circuit_breaker_chain.state.value,
            "circuit_breaker_mempool": self._circuit_breaker_mempool.state.value,
            "last_processed_slot": self._last_processed_slot,
            "last_ogmios_msg_at": self._last_msg_at.isoformat() if self._last_msg_at else None,
            "sync_lag_slots": sync_lag,
            # 1 slot ≈ 1 s on Cardano (mainnet, preprod, and preview all use 1s slot length)
            "sync_lag_seconds": sync_lag,
            "ws_url": self.ws_url,
        }
