"""Mempool monitoring via the Ogmios LocalTxMonitor mini-protocol.

Owns everything keyed to the mempool's view of the chain: the pending-tx
input-ref index used for front-running collision detection, the dedup set
over mempool snapshots, the cache of mempool-time UTxO input resolutions
consumed when a tx confirms, and the TTL/cap prune sweep over all of it.

The chain-sync side (app.ingestion.ogmios_client.OgmiosClient) interacts
with this state only through the public methods: record_displacements /
settle_confirmed on block confirmation, peek_enrichment / consume_enrichment
around its persistence checkpoint, and clear_on_rollback on rollBackward.
The durability-critical orderings (when enrichment is consumed relative to
save_sync_point) belong to the chain client, not here.

Per-instance dependencies are injected as the client's bound coroutines so
the WebSocket protocol helpers and the LocalStateQuery connection stay
owned by the client (one mini-protocol per connection, but one shared
JSON-RPC/telemetry layer).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

from app.analysis.features import extract_fee, extract_ttl
from app.config import settings
from app.db import postgres, raw_store
from app.ingestion.input_enrichment import parse_resolved_utxo
from app.ingestion.ogmios_parser import ogmios_input_ref
from app.ingestion.resilience import CircuitBreaker, ExponentialBackoff, run_with_reconnect
from app.models.transaction import NormalizedTransaction

logger = logging.getLogger(__name__)


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


class MempoolMonitor:
    """LocalTxMonitor loop plus the mempool-derived state it maintains."""

    def __init__(
        self,
        network: str,
        emit: Callable[[dict], Awaitable[None]],
        query_utxo: Callable[[List[dict]], Awaitable[List[dict]]],
        connect_ws: Callable[[str], Awaitable[object]],
        send_recv: Callable[..., Awaitable[dict]],
    ):
        self.network = network
        self._emit = emit
        self._query_utxo = query_utxo
        self._connect_ws = connect_ws
        self._send_recv = send_recv

        self._ws = None
        self._running = True  # set once here; stop() sets it False
        self.connected = False

        # Strong refs to fire-and-forget raw-store writes. asyncio only weakly
        # references tasks, so a bare create_task() can be GC'd before it runs
        # (the raw payload silently never persists). Keeping the ref pins the
        # task; the done-callback logs any failure (instead of swallowing it)
        # and drops the ref.
        self._bg_tasks: Set[asyncio.Task] = set()

        # Mempool deduplication (per-snapshot, cleared on reconnect and rollback)
        self._seen_mempool_txs: Set[str] = set()

        # Deterministic prune cadence: counts every processed mempool tx and
        # triggers the TTL/cap sweep at MEMPOOL_PRUNE_EVERY_N_TXS. The old
        # trigger (len(_seen_mempool_txs) % N == 0) stalled indefinitely
        # because the set length is not monotonic (confirms discard entries,
        # rollbacks and reconnects clear it), so the sweep could never fire
        # in a quiet mempool.
        self._mempool_txs_since_prune = 0

        # Front-running collision detection: pending-tx input-ref index
        # (see PendingTxIndex for the entry shape and complexity rationale).
        self._pending = PendingTxIndex()

        # Cache of resolved UTxO inputs for PENDING transactions.
        # Populated by the mempool monitor via queryLedgerState/utxo, consumed
        # by the chain client when the tx is confirmed in a block; popped
        # only AFTER the sync checkpoint advances (consume_enrichment), so a
        # retry-exhausted insert, a failed checkpoint write, or a rollback
        # replay re-parses with the enrichment intact.
        # Key: tx_hash, Value: ({(input_tx_hash, input_index): {address,
        # amount, assets}}, cached_at) — the timestamp lets the prune sweep
        # evict entries whose tx never reached the pending index (e.g.
        # collision tracking threw before track()), which previously leaked
        # until restart.
        self._pending_input_cache: Dict[str, Tuple[Dict[tuple, dict], datetime]] = {}

        # Resilience — own breaker so mempool failures stay isolated from
        # the chain-sync connection's.
        self._backoff = ExponentialBackoff(max_delay=settings.OGMIOS_RECONNECT_MAX_DELAY)
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=settings.OGMIOS_CIRCUIT_BREAKER_THRESHOLD,
            cooldown=settings.OGMIOS_CIRCUIT_BREAKER_COOLDOWN,
        )

    def _spawn_bg(self, coro, label: str) -> None:
        """Run a fire-and-forget coroutine with a retained ref and error logging.

        Replaces a bare asyncio.create_task(), which (a) can be GC'd before it
        finishes because asyncio holds tasks only weakly, and (b) swallows any
        exception. The ref lives in ``self._bg_tasks`` until the task ends; the
        callback logs a failure rather than dropping it silently.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("Background task %s failed: %r", label, t.exception())

        task.add_done_callback(_done)

    # --- Seams called by the chain-sync client ---

    def peek_enrichment(self, tx_hash: str) -> Optional[Tuple[Dict[tuple, dict], datetime]]:
        """Read-only enrichment lookup for block parsing; never consumes."""
        return self._pending_input_cache.get(tx_hash)

    def consume_enrichment(self, tx_hash: str) -> None:
        """Drop a tx's cached enrichment. Called only after the sync
        checkpoint advances; the chain client owns that ordering."""
        self._pending_input_cache.pop(tx_hash, None)

    def clear_on_rollback(self) -> None:
        """Clear the mempool dedup set so rolled-back txs can re-enter
        tracking if they re-appear in the mempool (valid in Cardano)."""
        self._seen_mempool_txs.clear()

    async def stop(self) -> None:
        """Stop the monitor loop and close its WebSocket."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
                logger.info("Ogmios [mempool]: disconnected")
            except Exception:
                pass
        self._ws = None

    @property
    def circuit_state(self) -> str:
        return self._circuit_breaker.state.value

    @staticmethod
    def _consumed_refs(tx: NormalizedTransaction) -> Set[tuple]:
        """The input refs a confirmed tx actually CONSUMED: the regular
        inputs for a validated tx, the collaterals for a phase-2-failed one
        (the ledger spends collateral on failure; the regular inputs stay
        live)."""
        if tx.script_valid:
            return {
                (inp.tx_hash, inp.index)
                for inp in tx.inputs
                if not inp.is_collateral and not inp.is_reference
            }
        return {(inp.tx_hash, inp.index) for inp in tx.inputs if inp.is_collateral}

    async def record_displacements(
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
                (_pending_refs, pending_seen, pending_fee, pending_addr, pending_ttl) = entry
                if shared:
                    delta_ms = (now_utc - pending_seen).total_seconds() * 1000
                    conf_addr = ""
                    conf_inp = next(
                        (
                            i
                            for i in tx.inputs
                            if not i.is_collateral and not i.is_reference and i.address
                        ),
                        None,
                    )
                    if conf_inp:
                        conf_addr = conf_inp.address
                    try:
                        await postgres.insert_mempool_collision(
                            tx_a=pending_id,
                            tx_b=tx.tx_hash,
                            network=self.network,
                            shared_inputs=[list(s) for s in shared],
                            shared_count=len(shared),
                            tx_a_seen_at=pending_seen,
                            tx_b_seen_at=now_utc,
                            delta_ms=delta_ms,
                            tx_a_fee=pending_fee,
                            tx_b_fee=tx.fee or 0,
                            tx_a_first_input_addr=pending_addr,
                            tx_b_first_input_addr=conf_addr,
                            tx_a_ttl=pending_ttl,
                            tx_b_ttl=0,
                        )
                        # Immediately mark outcome: the confirmed tx (tx_b) won
                        await postgres.update_collision_outcome(tx.tx_hash, self.network)
                        logger.info(
                            f"Displacement detected: pending {pending_id[:16]}.. "
                            f"displaced by confirmed {tx.tx_hash[:16]}.. "
                            f"({len(shared)} shared inputs)"
                        )
                    except Exception as e:
                        logger.debug(f"Displacement record error: {e}")

    async def settle_confirmed(
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
                await postgres.update_collision_outcome(tx.tx_hash, self.network)
            except Exception:
                pass

        for tx in normalized_txs:
            await self._emit(
                {
                    "eventType": "TX_CONFIRMED",
                    "txId": tx.tx_hash,
                    "network": self.network,
                    "observedAt": now.isoformat(),
                    "block": {"hash": block_id, "slot": block_slot, "height": block_height},
                }
            )

        for tx in normalized_txs:
            self._seen_mempool_txs.discard(tx.tx_hash)

    # --- LocalTxMonitor loop ---

    async def run(self):
        """Connect to Ogmios and run the LocalTxMonitor mini-protocol with resilience."""

        async def _connect():
            self._ws = await self._connect_ws("mempool")
            return self._ws

        async def _close():
            if self._ws:
                await self._ws.close()
                self._ws = None

        def _set_connected(value: bool) -> None:
            self.connected = value

        await run_with_reconnect(
            name="mempool",
            is_running=lambda: self._running,
            breaker=self._circuit_breaker,
            backoff=self._backoff,
            connect=_connect,
            run_session=self._mempool_loop,
            on_connected=_set_connected,
            close=_close,
            poll_seconds=settings.OGMIOS_CIRCUIT_OPEN_POLL_SECONDS,
            stable_reset_seconds=settings.OGMIOS_SESSION_STABLE_RESET_SECONDS,
        )

    async def _record_mempool_collisions(
        self,
        tx_id: str,
        tx_data: dict,
        now: datetime,
    ) -> None:
        """Record same-mempool input collisions for a newly seen pending tx,
        register it in the pending index, and run the throttled TTL prune.

        Same-mempool collisions are the rare path (a node rejects a second
        spend of the same UTxO); the common front-running signal is
        displacement, handled in record_displacements on the chain side.
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
            input_refs.add(ogmios_input_ref(inp))

        if input_refs:
            # Check for collisions with existing pending txs
            # (ref-index lookup: O(refs), not O(pending entries))
            for other_id, shared in self._pending.sharing(input_refs).items():
                # Skip self-matches. _seen_mempool_txs is cleared on reconnect,
                # rollback (clear_on_rollback), and when the dedup cap trips,
                # but _pending keeps its entries, so a re-observed tx finds
                # ITSELF in the ref index. Without this guard it would insert a
                # collision row with tx_a == tx_b (all inputs trivially
                # "shared"), feeding junk to the front_running scorer -- and at
                # mainnet rollback frequency, thousands of such rows. The
                # chain-side displacement check has the equivalent guard.
                if other_id == tx_id:
                    continue
                other_entry = self._pending.get(other_id)
                if other_entry is None:
                    continue
                (_other_refs, other_seen, other_fee, other_addr, other_ttl) = other_entry
                if shared:
                    delta_ms = (now - other_seen).total_seconds() * 1000
                    try:
                        await postgres.insert_mempool_collision(
                            tx_a=other_id,
                            tx_b=tx_id,
                            network=self.network,
                            shared_inputs=[list(s) for s in shared],
                            shared_count=len(shared),
                            tx_a_seen_at=other_seen,
                            tx_b_seen_at=now,
                            delta_ms=delta_ms,
                            tx_a_fee=other_fee,
                            tx_b_fee=tx_fee,
                            tx_a_first_input_addr=other_addr,
                            tx_b_first_input_addr=first_input_addr,
                            tx_a_ttl=other_ttl,
                            tx_b_ttl=tx_ttl,
                        )
                        logger.info(
                            f"Mempool collision: {other_id[:16]}.. vs {tx_id[:16]}.. "
                            f"({len(shared)} shared inputs, delta={delta_ms:.0f}ms)"
                        )
                    except Exception as e:
                        logger.error(f"Failed to record collision: {e}")

            self._pending.track(
                tx_id,
                (input_refs, now, tx_fee, first_input_addr, tx_ttl),
            )

        # Prune stale entries (throttled; knobs documented in config.py).
        # Deterministic per-call counter, NOT len(_seen_mempool_txs) % N: the
        # set length is non-monotonic (confirms discard entries, rollbacks
        # and reconnects clear it), so the modulo trigger could skip the
        # sweep indefinitely and the TTL/cap eviction would never run.
        self._mempool_txs_since_prune += 1
        if self._mempool_txs_since_prune >= settings.MEMPOOL_PRUNE_EVERY_N_TXS:
            self._mempool_txs_since_prune = 0
            cutoff = now - timedelta(seconds=settings.MEMPOOL_PENDING_TTL_SECONDS)
            for k in self._pending.stale_ids(cutoff):
                self._pending.untrack(k)
                self._pending_input_cache.pop(k, None)
            # Sweep cache entries by their own age too: an entry whose tx
            # never reached the pending index (collision tracking threw
            # before track()) is invisible to stale_ids and leaked forever.
            orphaned = [
                k for k, (_, cached_at) in self._pending_input_cache.items() if cached_at < cutoff
            ]
            for k in orphaned:
                self._pending_input_cache.pop(k, None)
            # Cap dedup set to prevent unbounded growth
            if len(self._seen_mempool_txs) > settings.MEMPOOL_SEEN_TXS_MAX:
                self._seen_mempool_txs.clear()

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
                output_refs.append(
                    {
                        "transaction": {"id": tx_obj["id"]},
                        "index": inp.get("index", 0),
                    }
                )

        if not output_refs:
            return

        utxos = await self._query_utxo(output_refs)
        if not utxos:
            return

        cache_entry: Dict[tuple, dict] = {}
        for utxo in utxos:
            ref, resolved = parse_resolved_utxo(utxo)
            cache_entry[ref] = resolved

        if cache_entry:
            self._pending_input_cache[tx_id] = (
                cache_entry,
                datetime.now(timezone.utc),
            )
            logger.debug(
                f"Resolved {len(cache_entry)}/{len(output_refs)} inputs for pending tx {tx_id}"
            )

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
                    self._spawn_bg(
                        raw_store.write_mempool(self.network, tx_id, tx_data, now),
                        label=f"raw_store.write_mempool({tx_id})",
                    )

                try:
                    await postgres.upsert_lifecycle_pending(
                        tx_id=tx_id,
                        network=self.network,
                        first_seen_at=now,
                    )
                except Exception as e:
                    logger.error(f"Error persisting pending tx {tx_id}: {e}")

                await self._emit(
                    {
                        "eventType": "TX_PENDING",
                        "txId": tx_id,
                        "network": self.network,
                        "observedAt": now.isoformat(),
                        "firstSeenAt": now.isoformat(),
                    }
                )

                logger.debug(f"TX_PENDING: {tx_id}")
