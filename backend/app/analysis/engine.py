"""Analysis Engine: multi-class orchestrator.

Reads unanalyzed transactions from ClickHouse, runs each enabled scorer's
gate/score pipeline, and writes a 9-element score vector per transaction to
the tx_class_scores table.

The public interface (run_once / run_once_async) is called by tasks/analysis.py.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.config import settings
from app.db import clickhouse, postgres
from app.analysis.normalise import score_to_band
from app.analysis.scorers.base import BaseScorer
from app.analysis.scorers.phishing import PhishingScorer
from app.analysis.scorers.token_dust import TokenDustScorer
from app.analysis.scorers.large_value import LargeValueScorer
from app.analysis.scorers.large_datum import LargeDatumScorer
from app.analysis.scorers.multiple_sat import MultipleSatScorer
from app.analysis.scorers.front_running import FrontRunningScorer
from app.analysis.scorers.sandwich import SandwichScorer
from app.analysis.scorers.circular import CircularScorer
from app.analysis.scorers.fake_token import FakeTokenScorer

logger = logging.getLogger(__name__)

# Version tag written to every result row
_VERSION = "phase4"

# All attack class names in canonical order
_CLASS_NAMES = [
    "token_dust", "large_value", "large_datum", "multiple_sat",
    "front_running", "sandwich", "circular", "fake_token", "phishing",
]


def _build_scorers() -> List[BaseScorer]:
    """Instantiate all enabled scorers.

    Scorers for not-yet-implemented classes are simply absent from the list;
    their score defaults to -1 (not applicable) in the output.
    """
    scorers: List[BaseScorer] = []

    if getattr(settings, "SCORER_TOKEN_DUST_ENABLED", True):
        scorers.append(TokenDustScorer())
    if getattr(settings, "SCORER_LARGE_VALUE_ENABLED", True):
        scorers.append(LargeValueScorer())
    if getattr(settings, "SCORER_LARGE_DATUM_ENABLED", True):
        scorers.append(LargeDatumScorer())
    if getattr(settings, "SCORER_MULTIPLE_SAT_ENABLED", True):
        scorers.append(MultipleSatScorer())
    if getattr(settings, "SCORER_FRONT_RUNNING_ENABLED", True):
        scorers.append(FrontRunningScorer())
    if getattr(settings, "SCORER_SANDWICH_ENABLED", True):
        scorers.append(SandwichScorer())
    if getattr(settings, "SCORER_CIRCULAR_ENABLED", True):
        scorers.append(CircularScorer())
    if getattr(settings, "SCORER_FAKE_TOKEN_ENABLED", True):
        scorers.append(FakeTokenScorer())
    if getattr(settings, "SCORER_PHISHING_ENABLED", True):
        scorers.append(PhishingScorer())

    return scorers


def _enrich_inputs_with_resolved_addresses(
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
        input_rows = clickhouse._get_client().execute(
            """SELECT tx_hash, input_index, address, amount
            FROM transaction_inputs
            WHERE tx_hash IN %(hashes)s
              AND network = %(network)s""",
            {"hashes": tx_hashes, "network": network},
        )
    except Exception:
        logger.warning("Failed to fetch resolved input addresses", exc_info=True)
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
    _MAX_REF_TXS = 2000
    if len(ref_tx_hashes) > _MAX_REF_TXS:
        logger.warning(
            "Capping ref tx lookups: %d -> %d", len(ref_tx_hashes), _MAX_REF_TXS,
        )
        ref_tx_hashes = set(list(ref_tx_hashes)[:_MAX_REF_TXS])

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


def _score_transaction(
    row: Dict[str, Any],
    scorers: List[BaseScorer],
) -> Dict[str, Any]:
    """Run all enabled scorers against a single transaction.

    Returns a dict ready for insert_class_scores().
    """
    tx_hash = row["tx_hash"]
    network = row["network"]
    now = datetime.now(timezone.utc)

    # Both metadata and raw_data are pre-parsed in run_once()
    metadata = row.get("metadata")
    raw_data = row.get("raw_data")

    # Build the features dict available to all scorers
    features: Dict[str, Any] = {
        "tx_hash": tx_hash,
        "network": network,
        "fee": row.get("fee", 0),
        "input_count": row.get("input_count", 0),
        "output_count": row.get("output_count", 0),
        "total_output_value": row.get("total_output_value", 0),
        "metadata": metadata,
        "addresses": row.get("addresses") or [],
        "raw_data": raw_data,
        "slot": row.get("slot"),
        "block_height": row.get("block_height"),
        "timestamp": row.get("timestamp"),
        # Phase 4 cross-tx enrichment data (injected by _enrich_* functions)
        "collision": row.get("collision"),
        "cycle": row.get("cycle"),
        "sandwich": row.get("sandwich"),
    }

    # Run each scorer
    scores: Dict[str, float] = {name: -1.0 for name in _CLASS_NAMES}
    sub_scores: Dict[str, Dict[str, float]] = {}

    for scorer in scorers:
        try:
            if scorer.gate(features):
                result = scorer.score(features)
                scores[scorer.name] = result.score
                sub_scores[scorer.name] = result.sub_scores
        except Exception:
            logger.exception(f"Scorer {scorer.name} failed on tx {tx_hash}")

    # Compute aggregate
    applicable = {k: v for k, v in scores.items() if v >= 0}
    if applicable:
        max_class = max(applicable, key=applicable.get)
        max_score = applicable[max_class]
    else:
        max_class = ""
        max_score = 0.0

    risk_band = score_to_band(max_score)

    return {
        "tx_hash": tx_hash,
        "network": network,
        **scores,
        "max_score": round(max_score, 2),
        "max_class": max_class,
        "risk_band": risk_band,
        "sub_scores": sub_scores,
        "analysis_version": _VERSION,
        "analyzed_at": now,
    }


def _enrich_sandwich_features(rows: List[Dict[str, Any]], network: str):
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


def _enrich_cycle_features(rows: List[Dict[str, Any]], network: str):
    """Enrich rows with cycle detection data for circular transfer scoring."""
    if not settings.SCORER_CIRCULAR_ENABLED or not settings.CYCLE_DETECTION_ENABLED:
        return
    try:
        from app.analysis.graph import detect_cycle
    except ImportError:
        return

    for row in rows:
        # Pre-filter: skip txs with many outputs (unlikely circular)
        output_count = row.get("output_count", 0)
        if output_count > 20:
            continue
        try:
            cycle = detect_cycle(row["tx_hash"], network)
            if cycle:
                row["cycle"] = cycle
        except Exception as e:
            logger.debug(f"Cycle detection failed for {row['tx_hash'][:16]}: {e}")


def _enrich_collision_features(rows: List[Dict[str, Any]], network: str):
    """Enrich rows with mempool collision data for front-running detection.

    Called from the sync run_once() context, uses asyncio to call async postgres.
    """
    if not settings.SCORER_FRONT_RUNNING_ENABLED:
        return
    tx_hashes = [r["tx_hash"] for r in rows]
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            future = asyncio.run_coroutine_threadsafe(
                postgres.get_collisions_for_txs(tx_hashes, network), loop
            )
            collisions = future.result(timeout=30)
        else:
            collisions = loop.run_until_complete(
                postgres.get_collisions_for_txs(tx_hashes, network)
            )
    except Exception as e:
        logger.warning(f"Collision enrichment failed (non-fatal): {e}")
        return

    for row in rows:
        collision = collisions.get(row["tx_hash"])
        if collision:
            row["collision"] = collision


def run_once(network: str) -> int:
    """Score one batch of unanalyzed transactions.  Returns the count scored."""
    if not settings.ANALYSIS_ENABLED:
        return 0

    rows = clickhouse.get_unanalyzed_transactions(
        network, settings.ANALYSIS_ENGINE_BATCH_SIZE
    )
    if not rows:
        return 0

    scorers = _build_scorers()

    # Pre-parse JSON string fields so scorers receive dicts, not strings
    for row in rows:
        rd = row.get("raw_data")
        if isinstance(rd, str) and rd:
            try:
                row["raw_data"] = json.loads(rd)
            except (json.JSONDecodeError, TypeError):
                row["raw_data"] = None
        md = row.get("metadata")
        if isinstance(md, str) and md not in ("", "{}"):
            try:
                row["metadata"] = json.loads(md)
            except (json.JSONDecodeError, TypeError):
                row["metadata"] = None
        elif not md or md == "{}":
            row["metadata"] = None

    # Enrich inputs with resolved addresses from transaction_inputs table
    _enrich_inputs_with_resolved_addresses(rows, network)

    # Enrich collision features for front-running detection
    _enrich_collision_features(rows, network)

    # Enrich cycle features for circular transfer detection
    _enrich_cycle_features(rows, network)

    # Enrich sandwich features for structural sandwich detection
    _enrich_sandwich_features(rows, network)

    results = []
    for row in rows:
        result = _score_transaction(row, scorers)
        results.append(result)

    clickhouse.insert_class_scores(results)

    # Log summary
    critical = sum(1 for r in results if r["risk_band"] == "Critical")
    high = sum(1 for r in results if r["risk_band"] == "High")
    scored_classes = {}
    for r in results:
        for cls in _CLASS_NAMES:
            if r.get(cls, -1) >= 0:
                scored_classes[cls] = scored_classes.get(cls, 0) + 1

    class_summary = ", ".join(f"{k}={v}" for k, v in scored_classes.items()) or "none"
    logger.info(
        f"Analysis Engine [{network}]: scored {len(results)} txs "
        f"(Critical={critical}, High={high}) classes: {class_summary}"
    )
    return len(results)


async def run_once_async(network: str) -> int:
    """Non-blocking wrapper: runs run_once on the dedicated ClickHouse executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(clickhouse._ch_executor, run_once, network)
