"""Transfer graph cycle detection for the Circular scorer.

Performs a bounded BFS forward from a transaction's sender addresses to detect
value cycles (ADA returning to the origin within max_hops).  Queries
transaction_inputs and transaction_outputs in ClickHouse.
"""

import logging
import math
import statistics
from collections import Counter
from typing import Any, Dict, List, Optional, Set

from app.analysis.scorer_config import get as _get_cfg
from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

_CIRCULAR_CFG = _get_cfg("circular")
_CYCLE_CFG = _CIRCULAR_CFG["cycle"]
_MAX_AGE_SLOTS = int(_CYCLE_CFG["max_age_slots"])
_MAX_OUTPUT_FANOUT = int(_CYCLE_CFG["max_output_fanout"])
# Public alias: the engine's cycle pre-filter must key off the same knob so
# the two sites cannot drift (the engine previously hardcoded the value).
MAX_OUTPUT_FANOUT = _MAX_OUTPUT_FANOUT
_RECURRENCE_WINDOW_DAYS = int(_CIRCULAR_CFG["recurrence_window_days"])


def _first_sorted(addresses) -> str:
    """Pick a deterministic representative address from an iterable.

    Set iteration order is unstable across Python processes (string hash
    randomization), so picking via ``next(iter(...))`` produces different
    representatives on the same input across runs. Sorting by bech32 string
    gives a stable, meaningless-to-the-operator default.
    """
    if not addresses:
        return ""
    return sorted(addresses)[0]


def detect_cycle(
    tx_hash: str,
    network: str,
    max_hops: int = 0,
) -> Optional[Dict]:
    """Detect if tx_hash is part of a value cycle returning to origin.

    Returns a dict matching the circular scorer's expected structure, or None.
    """
    if max_hops <= 0:
        max_hops = settings.CYCLE_MAX_HOPS
    max_fanout = settings.CYCLE_MAX_FANOUT
    client = clickhouse._get_client()

    # Step 1: Get sender addresses (input addresses of this tx)
    rows = client.execute(
        """
        SELECT DISTINCT address
        FROM transaction_inputs
        WHERE tx_hash = %(tx_hash)s
          AND network = %(network)s
          AND is_collateral = 0
          AND is_reference = 0
          AND is_unspent_attempt = 0
          AND address != ''
        """,
        {"tx_hash": tx_hash, "network": network},
    )
    origin_addresses: Set[str] = {r[0] for r in rows}
    if not origin_addresses:
        return None

    # Step 2: Get output addresses and amounts of this tx.
    # FINAL on both sides: origin_amount is summed from these rows, and a
    # not-yet-merged ReplacingMergeTree duplicate (or a duplicate
    # transactions row multiplying the join) would double it.
    out_rows = client.execute(
        """
        SELECT o.address, o.amount, t.slot
        FROM (
            SELECT tx_hash, network, address, amount
            FROM transaction_outputs FINAL
            WHERE tx_hash = %(tx_hash)s
              AND network = %(network)s
              AND is_collateral = 0
        ) o
        JOIN (
            SELECT tx_hash, network, slot
            FROM transactions FINAL
            WHERE tx_hash = %(tx_hash)s AND network = %(network)s
        ) t ON o.tx_hash = t.tx_hash AND o.network = t.network
        """,
        {"tx_hash": tx_hash, "network": network},
    )
    if not out_rows:
        return None

    origin_slot = out_rows[0][2] if out_rows else 0

    # origin_amount excludes change (outputs returning to sender)
    origin_amount = sum(r[1] for r in out_rows if r[0] not in origin_addresses)

    # Recipients of this tx (excluding change back to origin)
    current_addresses: Set[str] = {r[0] for r in out_rows if r[0] not in origin_addresses}
    if not current_addresses:
        return None

    # Pre-filter: skip txs with too many output addresses (unlikely circular).
    # Threshold tunable via circular.cycle.max_output_fanout.
    if len(current_addresses) > _MAX_OUTPUT_FANOUT:
        return None

    # Step 3: Bounded BFS forward
    visited_addresses: Set[str] = set(origin_addresses) | set(current_addresses)
    all_cycle_addresses: List[str] = list(origin_addresses)
    # ``hops`` is the single source of truth for per-step amounts/slots.
    # The stats math in ``_build_cycle_result`` derives ``hop_amounts`` and
    # ``hop_slots`` from this list, so they never get out of sync.
    # ``_first_sorted`` picks a deterministic representative address per
    # step: set iteration order varies across processes (string hash
    # randomization) and would otherwise make evidence non-reproducible.
    origin_repr = _first_sorted(origin_addresses)
    hops: List[Dict[str, Any]] = [
        {"address": origin_repr, "amount_lovelace": origin_amount, "slot": origin_slot}
    ]

    for hop in range(1, max_hops + 1):
        if not current_addresses:
            break

        # Deterministic frontier: set iteration order varies across Python
        # processes (string hash randomization), so an unsorted truncation
        # would explore a different address subset run-to-run and silently
        # miss cycles through the dropped legs. Same rationale as
        # _first_sorted, applied to the cap that actually controls recall.
        addr_list = sorted(current_addresses)[:max_fanout]

        # Find txs where these addresses are inputs (they spent received funds).
        # The slot window is bounded: cycles spanning >24h are almost always
        # incidental reuses of an address, not deliberate layering.
        next_rows = client.execute(
            """
            SELECT DISTINCT ti.tx_hash, to2.address, to2.amount, t.slot
            FROM transaction_inputs ti
            JOIN transaction_outputs to2
                ON ti.tx_hash = to2.tx_hash AND ti.network = to2.network
            JOIN transactions t
                ON ti.tx_hash = t.tx_hash AND ti.network = t.network
            WHERE ti.address IN %(addresses)s
              AND ti.network = %(network)s
              AND ti.is_collateral = 0
              AND ti.is_reference = 0
              AND ti.is_unspent_attempt = 0
              AND to2.is_collateral = 0
              AND t.slot >= %(min_slot)s
              AND t.slot <= %(max_slot)s
              AND ti.tx_hash != %(origin_tx)s
            ORDER BY t.slot ASC
            LIMIT 500
            """,
            {
                "addresses": addr_list,
                "network": network,
                "min_slot": origin_slot,
                "max_slot": (origin_slot or 0) + _MAX_AGE_SLOTS,
                "origin_tx": tx_hash,
            },
        )

        if not next_rows:
            break

        next_addresses: Set[str] = set()
        hop_amount = 0
        hop_slot = 0
        for r in next_rows:
            out_addr, out_amt, slot = r[1], r[2], r[3]
            hop_slot = max(hop_slot, slot)

            # Check if cycle detected (output goes back to origin)
            if out_addr in origin_addresses:
                # Cycle found
                all_cycle_addresses.extend(list(current_addresses))
                all_cycle_addresses.append(out_addr)
                hops.append(
                    {"address": out_addr, "amount_lovelace": out_amt, "slot": slot}
                )

                return _build_cycle_result(
                    cycle_length=hop + 1,
                    addresses=all_cycle_addresses,
                    origin_amount=origin_amount,
                    final_amount=out_amt,
                    hops=hops,
                    origin_addresses=origin_addresses,
                    tx_hash=tx_hash,
                    network=network,
                )

            if out_addr not in visited_addresses:
                next_addresses.add(out_addr)
                hop_amount += out_amt

        all_cycle_addresses.extend(list(current_addresses))
        hop_repr = _first_sorted(current_addresses)
        hops.append(
            {"address": hop_repr, "amount_lovelace": hop_amount, "slot": hop_slot}
        )
        visited_addresses |= next_addresses
        current_addresses = next_addresses

    return None


def _count_origin_recurrence(
    origin_address: str,
    network: str,
    exclude_tx: str,
) -> int:
    """Count prior transactions from the same origin that were scored as circular.

    Queries tx_class_scores joined with transaction_inputs to find how many
    previous cycles originated from this address within a rolling window
    (per Polimi spec Section 5.3, default 30 days, tunable via
    circular.recurrence_window_days).  This feeds the cycle_recurrence
    sub-score (30% weight in the CircularScorer).

    Only counts ancestors scored High or above (>=60). Counting every tx with
    circular > 0 self-reinforces: once a single tx scored non-zero, every
    subsequent tx from the same origin got a recurrence boost, cascading
    false positives. High+ is the signal we want to amplify.
    """
    if not origin_address:
        return 0
    try:
        client = clickhouse._get_client()
        rows = client.execute(
            """
            SELECT count(DISTINCT s.tx_hash) AS cnt
            FROM tx_class_scores s FINAL
            JOIN transaction_inputs ti
                ON s.tx_hash = ti.tx_hash AND s.network = ti.network
            WHERE ti.address = %(origin)s
              AND s.network = %(network)s
              AND s.circular >= 60
              AND s.tx_hash != %(exclude)s
              AND s.analyzed_at >= now() - INTERVAL %(window)s DAY
              AND ti.is_collateral = 0
              AND ti.is_reference = 0
              AND ti.is_unspent_attempt = 0
            """,
            {
                "origin": origin_address,
                "network": network,
                "exclude": exclude_tx,
                "window": _RECURRENCE_WINDOW_DAYS,
            },
        )
        return rows[0][0] if rows else 0
    except Exception as e:
        logger.debug(f"Recurrence count query failed for {origin_address[:16]}: {e}")
        return 0


def _build_cycle_result(
    cycle_length: int,
    addresses: List[str],
    origin_amount: int,
    final_amount: int,
    hops: List[Dict[str, Any]],
    origin_addresses: Set[str],
    tx_hash: str = "",
    network: str = "",
) -> Dict:
    """Build the cycle dict expected by the CircularScorer.

    ``hops`` is the single source of truth for per-step amounts and slots;
    we derive ``hop_amounts`` / ``hop_slots`` from it for the stats math
    below. ``addresses`` is kept as a separate parameter because the
    entropy calculation needs the full per-step address list (which may
    include duplicates and is longer than ``hops`` when a step had
    multiple recipients), not just the per-hop representative.
    """
    hop_amounts = [int(h.get("amount_lovelace", 0)) for h in hops]
    hop_slots = [int(h.get("slot", 0)) for h in hops]

    # Amount similarity: 1 - CV(hop_amounts) (coefficient of variation)
    if len(hop_amounts) >= 2:
        mean_amt = statistics.mean(hop_amounts)
        if mean_amt > 0:
            cv = statistics.stdev(hop_amounts) / mean_amt
            amount_similarity = max(0.0, min(1.0, 1.0 - cv))
        else:
            amount_similarity = 0.0
    elif hop_amounts and hop_amounts[0] > 0:
        amount_similarity = 1.0
    else:
        amount_similarity = 0.0

    # Net loss ratio: how much value was lost (fees)
    if origin_amount > 0:
        net_loss_ratio = max(0, origin_amount - final_amount) / origin_amount
    else:
        net_loss_ratio = 1.0

    # Recipient entropy: Shannon entropy of address frequency distribution
    addr_counts = Counter(addresses)
    n_total = len(addresses)
    n_unique = len(addr_counts)
    if n_unique > 1 and n_total > 0:
        entropy = -sum(
            (c / n_total) * math.log2(c / n_total)
            for c in addr_counts.values()
        )
        max_entropy = math.log2(n_unique)
        entropy = entropy / max_entropy if max_entropy > 0 else 0.0
    else:
        entropy = 0.0

    # Round amount flag: origin amount is a round number (divisible by 1 ADA)
    round_amount_flag = origin_amount > 0 and origin_amount % 1_000_000 == 0

    # Temporal concentration: fraction of hops within a tight slot window
    if len(hop_slots) >= 2:
        total_span = max(hop_slots) - min(hop_slots)
        if total_span > 0:
            temporal_concentration = cycle_length / total_span
        else:
            temporal_concentration = 1.0
    else:
        temporal_concentration = 0.0

    # Mean inter-hop delta in slots
    if len(hop_slots) >= 2:
        deltas = [hop_slots[i + 1] - hop_slots[i] for i in range(len(hop_slots) - 1) if hop_slots[i + 1] > hop_slots[i]]
        mean_delta = sum(deltas) / len(deltas) if deltas else 100.0
    else:
        mean_delta = 100.0

    return {
        "cycle_length": cycle_length,
        "addresses": list(set(addresses))[:20],
        "hops": hops,
        "amount_similarity": round(amount_similarity, 4),
        "net_loss_ratio": round(net_loss_ratio, 4),
        "recurrence_count": _count_origin_recurrence(
            list(origin_addresses)[0] if origin_addresses else "",
            network,
            tx_hash,
        ),
        "recipient_entropy": round(entropy, 4),
        "round_amount_flag": round_amount_flag,
        "temporal_concentration": round(min(temporal_concentration, 1.0), 4),
        "mean_inter_hop_delta_slots": round(mean_delta, 2),
        "origin_cluster": list(origin_addresses)[0] if origin_addresses else "__unknown__",
    }
