"""Cross-transaction enrichment for the analysis engine.

Split out of ``app.analysis.engine``: each function here decorates a batch
of unanalyzed-transaction rows in place with data a scorer needs but the
row itself does not carry (resolved input addresses, mempool collisions,
transfer-graph cycles, structural sandwich patterns). The engine stays the
orchestrator; this module owns the per-feature fetch logic.

All functions run inside run_once() on a ClickHouse worker thread, never on
the event loop; collision enrichment bridges back to the loop captured via
:func:`set_main_loop`.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from app.config import settings
from app.db import clickhouse, postgres

logger = logging.getLogger(__name__)

# Transient per-row key: the names of enrichment steps that FAILED (raised) for
# this row, as opposed to running and finding nothing. The engine reads it to
# decide whether to defer the tx (retry) rather than score an affected class as
# a silent no-signal. Consumed and popped by engine._handle_incomplete_scoring.
_ENRICHMENT_FAILED_KEY = "_enrichment_failed"


def _mark_enrichment_failed(row: Dict[str, Any], name: str) -> None:
    """Record that enrichment ``name`` failed for ``row`` (idempotent)."""
    failed = row.setdefault(_ENRICHMENT_FAILED_KEY, [])
    if name not in failed:
        failed.append(name)


def enrich_inputs_with_resolved_addresses(
    rows: List[Dict[str, Any]],
    network: str,
) -> None:
    """Inject resolved input addresses from transaction_inputs into raw_data.

    Ogmios v6 raw_data only contains input references (tx_hash + index) without
    addresses.  Scorers like multiple_sat need input addresses to group by script.
    The resolved addresses are stored in the transaction_inputs ClickHouse table
    at ingestion time, so we batch-fetch them and patch raw_data in-place.

    Both queries are scoped by network to prevent cross-network pollution when
    multiple instances (e.g. preprod + preview) share the same ClickHouse.
    """
    tx_hashes = [r["tx_hash"] for r in rows if r.get("raw_data")]
    if not tx_hashes:
        return

    try:
        # Deliberately NOT filtered on is_unspent_attempt: the patch below is
        # keyed by input_index against raw_data["inputs"], and a failed tx's
        # attempted inputs occupy indices 0..k there (parser emits them
        # first), so the alignment depends on fetching every row.
        input_rows = clickhouse._get_client().execute(
            """SELECT tx_hash, input_index, address, amount
            FROM transaction_inputs
            WHERE tx_hash IN %(hashes)s
              AND network = %(network)s""",
            {"hashes": tx_hashes, "network": network},
        )
    except Exception:
        logger.warning("Failed to fetch resolved input addresses", exc_info=True)
        # Recall-safe: mark every row so the engine defers rather than scoring
        # multiple_sat (which groups by resolved input address) as no-signal.
        for r in rows:
            _mark_enrichment_failed(r, "input_addresses")
        return

    # Build lookup: tx_hash -> {input_index -> (address, amount)}
    lookup: Dict[str, Dict[int, tuple]] = {}
    for tx_h, idx, addr, amt in input_rows:
        lookup.setdefault(tx_h, {})[idx] = (addr, amt)

    # Collect referenced tx hashes to resolve input values from their outputs
    ref_tx_hashes = set()
    for row in rows:
        rd = row.get("raw_data")
        if isinstance(rd, dict):
            for inp in rd.get("inputs", []):
                ref = inp.get("transaction", {}).get("id")
                if ref:
                    ref_tx_hashes.add(ref)

    # Fetch raw_data of referenced txs to extract output values
    # Cap to avoid oversized IN clauses; remaining inputs just won't have values
    max_ref_txs = settings.ANALYSIS_MAX_REF_TXS
    if len(ref_tx_hashes) > max_ref_txs:
        logger.warning(
            "Capping ref tx lookups: %d -> %d", len(ref_tx_hashes), max_ref_txs,
        )
        ref_tx_hashes = set(sorted(ref_tx_hashes)[:max_ref_txs])

    ref_outputs: Dict[str, Dict[int, Dict]] = {}  # ref_tx -> {index -> output}
    if ref_tx_hashes:
        try:
            ref_rows = clickhouse._get_client().execute(
                "SELECT tx_hash, raw_data FROM transactions "
                "WHERE tx_hash IN %(hashes)s AND network = %(network)s",
                {"hashes": list(ref_tx_hashes), "network": network},
            )
            for ref_hash, ref_rd in ref_rows:
                if isinstance(ref_rd, str):
                    ref_rd = json.loads(ref_rd)
                if isinstance(ref_rd, dict):
                    for i, out in enumerate(ref_rd.get("outputs", [])):
                        ref_outputs.setdefault(ref_hash, {})[i] = out
        except Exception:
            logger.warning("Failed to fetch referenced tx outputs", exc_info=True)

    for row in rows:
        tx_hash = row["tx_hash"]
        rd = row.get("raw_data")
        if not isinstance(rd, dict):
            continue
        addr_map = lookup.get(tx_hash, {})
        for i, inp in enumerate(rd.get("inputs", [])):
            if "address" not in inp and i in addr_map:
                inp["address"] = addr_map[i][0]
            # Resolve input value from the referenced output
            if "value" not in inp:
                ref_hash = inp.get("transaction", {}).get("id")
                ref_idx = inp.get("index")
                if ref_hash and ref_idx is not None:
                    ref_out = ref_outputs.get(ref_hash, {}).get(ref_idx)
                    if ref_out and "value" in ref_out:
                        inp["value"] = ref_out["value"]


def enrich_sandwich_features(rows: List[Dict[str, Any]], network: str):
    """Enrich rows with structural sandwich pattern detection."""
    if not settings.SCORER_SANDWICH_ENABLED or not settings.SANDWICH_SIMPLIFIED_ENABLED:
        return
    try:
        from app.analysis.dex import detect_sandwich_pattern
    except ImportError:
        return

    for row in rows:
        slot = row.get("slot", 0)
        if not slot:
            continue
        try:
            sw = detect_sandwich_pattern(row["tx_hash"], network, slot)
            if sw:
                row["sandwich"] = sw
        except Exception as e:
            logger.debug(f"Sandwich detection failed for {row['tx_hash'][:16]}: {e}")
            _mark_enrichment_failed(row, "sandwich")


def enrich_cycle_features(rows: List[Dict[str, Any]], network: str):
    """Enrich rows with cycle detection data for circular transfer scoring."""
    if not settings.SCORER_CIRCULAR_ENABLED or not settings.CYCLE_DETECTION_ENABLED:
        return
    try:
        from app.analysis import graph as graph_mod
    except ImportError:
        return

    for row in rows:
        # Pre-filter: skip txs with many outputs (unlikely circular).
        # Same knob as detect_cycle's own fan-out gate
        # (circular.cycle.max_output_fanout) so the two sites cannot drift.
        output_count = row.get("output_count", 0)
        if output_count > graph_mod.MAX_OUTPUT_FANOUT:
            continue
        try:
            cycle = graph_mod.detect_cycle(row["tx_hash"], network)
            if cycle:
                row["cycle"] = cycle
        except Exception as e:
            logger.debug(f"Cycle detection failed for {row['tx_hash'][:16]}: {e}")
            _mark_enrichment_failed(row, "cycle")


# Captured by engine.run_once_async() so that enrich_collision_features
# (running on a clickhouse worker thread) can schedule async postgres calls
# back onto the main event loop. Module-level mutable state assumes a single
# asyncio loop per process, which matches our production deployment. Tests
# that drive the engine directly without run_once_async() will see collision
# enrichment skipped (debug log emitted); call set_main_loop() manually if
# that path matters for the test.
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Set or clear the captured main event loop (engine startup / tests)."""
    global _main_loop
    _main_loop = loop


def enrich_collision_features(rows: List[Dict[str, Any]], network: str):
    """Enrich rows with mempool collision data for front-running detection.

    Called from the sync run_once() context (on a clickhouse worker thread),
    schedules the async postgres call on the main event loop captured by
    run_once_async().
    """
    if not settings.SCORER_FRONT_RUNNING_ENABLED:
        return
    loop = _main_loop
    if loop is None or not loop.is_running():
        logger.debug("Collision enrichment skipped: main event loop unavailable")
        return
    tx_hashes = [r["tx_hash"] for r in rows]
    try:
        future = asyncio.run_coroutine_threadsafe(
            postgres.get_collisions_for_txs(tx_hashes, network), loop
        )
        collisions = future.result(timeout=30)
    except Exception as e:
        logger.warning(f"Collision enrichment failed (non-fatal): {e}")
        # Recall-safe: mark every row so the engine defers rather than scoring
        # front_running (which reads mempool collisions) as no-signal. The
        # loop-unavailable / feature-disabled early returns above are NOT
        # failures and deliberately do not mark.
        for r in rows:
            _mark_enrichment_failed(r, "collision")
        return

    for row in rows:
        collision = collisions.get(row["tx_hash"])
        if collision:
            row["collision"] = collision
