"""Transfer graph cycle detection for the Circular scorer.

Performs a bounded BFS forward from a transaction's sender addresses to detect
value cycles (ADA returning to the origin within max_hops).  Queries
transaction_inputs and transaction_outputs in ClickHouse.
"""

import logging
import math
import statistics
from collections import Counter
from typing import Any

from app.analysis.features import LOVELACE_PER_ADA
from app.analysis.scorer_config import get as _get_cfg
from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

_CIRCULAR_CFG = _get_cfg("circular")
_CYCLE_CFG = _CIRCULAR_CFG["cycle"]
_MAX_AGE_SLOTS = int(_CYCLE_CFG["max_age_slots"])
_MAX_OUTPUT_FANOUT = int(_CYCLE_CFG["max_output_fanout"])
# Per-hop row cap on the forward BFS scan. Config-backed (not an inline literal)
# because it controls recall: rows are ordered by slot ASC so a truncation keeps
# the earliest legs, but a hub hop with more than this many spends loses the
# tail. Raise in config if real layering is missed; never lower for precision.
_BFS_HOP_ROW_LIMIT = int(_CYCLE_CFG["bfs_hop_row_limit"])
# Fallback inter-hop delta (slots) when hop timing is unmeasurable. Shared with
# the circular scorer via config so the feature and the read cannot drift.
_DEFAULT_INTER_HOP_DELTA_SLOTS = int(_CYCLE_CFG["default_inter_hop_delta_slots"])
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
) -> dict | None:
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
    origin_addresses: set[str] = {r[0] for r in rows}
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

    # transactions.slot is Nullable(UInt64), so this can come back as None.
    # Coerce once, here: the hop query binds it to both ends of the slot window,
    # and `slot >= NULL` matches nothing, which silently skipped the entire
    # cycle search for any tx ingested without a slot.
    origin_slot = out_rows[0][2] or 0

    # origin_amount excludes change (outputs returning to sender)
    origin_amount = sum(r[1] for r in out_rows if r[0] not in origin_addresses)

    # Recipients of this tx (excluding change back to origin)
    current_addresses: set[str] = {r[0] for r in out_rows if r[0] not in origin_addresses}
    if not current_addresses:
        return None

    # Pre-filter: skip txs with too many output addresses (unlikely circular).
    # Threshold tunable via circular.cycle.max_output_fanout.
    if len(current_addresses) > _MAX_OUTPUT_FANOUT:
        return None

    # Step 3: Bounded BFS forward
    visited_addresses: set[str] = set(origin_addresses) | set(current_addresses)
    all_cycle_addresses: list[str] = list(origin_addresses)
    # ``hops`` is the single source of truth for per-step amounts/slots.
    # The stats math in ``_build_cycle_result`` derives ``hop_amounts`` and
    # ``hop_slots`` from this list, so they never get out of sync.
    # ``_first_sorted`` picks a deterministic representative address per
    # step: set iteration order varies across processes (string hash
    # randomization) and would otherwise make evidence non-reproducible.
    origin_repr = _first_sorted(origin_addresses)
    hops: list[dict[str, Any]] = [
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
        # Shape matters as much as the filters here. Joining the full
        # transaction_outputs and transactions tables makes ClickHouse build a
        # hash table over both in their entirety on every hop (~5.4M rows read
        # per call, ~950MB peak), because neither join side can inherit the
        # address filter. Narrowing to the candidate tx_hashes first (the slot
        # window is the selective predicate, ~24h of txs) and only then
        # resolving outputs collapses peak memory ~30x and cuts CPU ~1.5x
        # (40 real production hops replayed interleaved against one snapshot:
        # 935MB -> 31MB peak, 52.1 -> 34.0 CPU-seconds). The memory figure is
        # the robust one and the reason this matters: ~950MB allocations against
        # a 4GB container cap were driving the CPU spikes. Treat the CPU ratio as
        # cache-dependent; an uncontrolled cold-vs-warm comparison flatters it to
        # ~4x. Note rows-read goes UP, not down, since a streamed filtered scan
        # beats a giant hash build, so compare OSCPUVirtualTimeMicroseconds and
        # memory_usage, never read_rows, when retuning this.
        #
        # The matched row SET is unchanged: 35/40 replayed hops byte-identical to
        # the old shape, the other 5 differing only where the LIMIT truncates
        # (see the tiebreaker note below). The row ORDER is deliberately changed.
        #
        # The ORDER BY carries a full tiebreaker (tx_hash, address, amount)
        # rather than slot alone. Slot ties are common (many spends land in one
        # block), so ordering by slot alone left the LIMIT truncation picking an
        # arbitrary subset of equally-ranked rows: the same hop could explore a
        # different frontier run-to-run and miss a cycle through the dropped
        # legs. Same reproducibility rationale as _first_sorted above.
        #
        # Which origin-paying row sorts first no longer decides how much came
        # back. The loop stops at that row only to learn WHICH transaction
        # closes the cycle; _returned_to_origin then totals that transaction's
        # outputs to the whole origin set. No row order could have done this:
        # amount DESC still let dust to a second origin address, sorted ahead
        # by address, stand in for the repayment. amount keeps its DESC
        # tiebreak only for a total, reproducible order.
        next_rows = client.execute(
            """
            WITH cand AS (
                SELECT DISTINCT ti.tx_hash AS tx_hash, t.slot AS slot
                FROM transaction_inputs ti
                JOIN (
                    SELECT tx_hash, slot
                    FROM transactions
                    WHERE network = %(network)s
                      AND slot >= %(min_slot)s
                      AND slot <= %(max_slot)s
                ) t ON ti.tx_hash = t.tx_hash
                WHERE ti.address IN %(addresses)s
                  AND ti.network = %(network)s
                  AND ti.is_collateral = 0
                  AND ti.is_reference = 0
                  AND ti.is_unspent_attempt = 0
                  AND ti.tx_hash != %(origin_tx)s
            )
            SELECT DISTINCT c.tx_hash, to2.address, to2.amount, c.slot
            FROM cand c
            JOIN (
                SELECT tx_hash, address, amount
                FROM transaction_outputs
                WHERE network = %(network)s
                  AND is_collateral = 0
                  AND tx_hash IN (SELECT tx_hash FROM cand)
            ) to2 ON c.tx_hash = to2.tx_hash
            ORDER BY c.slot ASC, c.tx_hash ASC, to2.address ASC, to2.amount DESC
            LIMIT %(hop_row_limit)s
            """,
            {
                "addresses": addr_list,
                "network": network,
                "min_slot": origin_slot,
                "max_slot": origin_slot + _MAX_AGE_SLOTS,
                "origin_tx": tx_hash,
                "hop_row_limit": _BFS_HOP_ROW_LIMIT,
            },
        )

        if not next_rows:
            break

        next_addresses: set[str] = set()
        hop_amount = 0
        hop_slot = 0
        for r in next_rows:
            out_addr, out_amt, slot = r[1], r[2], r[3]
            hop_slot = max(hop_slot, slot)

            # Check if cycle detected (output goes back to origin)
            if out_addr in origin_addresses:
                # Cycle found. This row identifies the closing transaction; the
                # amount it returned is that transaction's total to the origin
                # set, not this one output (see _returned_to_origin).
                returned = _returned_to_origin(client, network, r[0], origin_addresses, out_amt)
                all_cycle_addresses.extend(list(current_addresses))
                all_cycle_addresses.append(out_addr)
                hops.append({"address": out_addr, "amount_lovelace": returned, "slot": slot})

                return _build_cycle_result(
                    cycle_length=hop + 1,
                    addresses=all_cycle_addresses,
                    origin_amount=origin_amount,
                    final_amount=returned,
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
        hops.append({"address": hop_repr, "amount_lovelace": hop_amount, "slot": hop_slot})
        visited_addresses |= next_addresses
        current_addresses = next_addresses

    return None


def _returned_to_origin(
    client: Any,
    network: str,
    closing_tx: str,
    origin_addresses: set[str],
    seen_amount: int,
) -> int:
    """Total lovelace the closing transaction pays back to ANY origin address.

    The hop query answers whether a cycle closes, not how much came back. Its
    rows are DISTINCT on (tx_hash, address, amount, slot) and the loop stops at
    the first origin-paying row, so using that row's amount let a closing leg
    hide its repayment two ways: a dust output to one origin address sorted
    ahead of the real repayment to another, or the repayment split into equal
    outputs that DISTINCT collapses into one. Either pushed net_loss_ratio
    toward 1.0 and CircularScorer's hard gate discarded a genuine cycle.
    Totalling every non-collateral output of the closing transaction that pays
    the origin set closes both, and extra outputs can only lower the measured
    loss, never raise it.

    FINAL for the same reason origin_amount uses it: a not-yet-merged duplicate
    row would otherwise double the total. If the lookup fails, the amount
    already matched is kept, which is what the cycle was measured with before,
    so a transient error cannot cost the finding.
    """
    try:
        rows = client.execute(
            """
            SELECT sum(amount)
            FROM transaction_outputs FINAL
            WHERE network = %(network)s
              AND tx_hash = %(tx_hash)s
              AND address IN %(origin)s
              AND is_collateral = 0
            """,
            {"network": network, "tx_hash": closing_tx, "origin": sorted(origin_addresses)},
        )
    except Exception:
        logger.warning(
            "Closing-leg total failed for %s; keeping the matched output",
            closing_tx[:16],
            exc_info=True,
        )
        return seen_amount
    total = rows[0][0] if rows and rows[0][0] is not None else 0
    # The matched output is one of the summed rows, so the total cannot be
    # smaller unless the two reads disagree; never report less than was seen.
    return max(int(total), seen_amount)


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
    addresses: list[str],
    origin_amount: int,
    final_amount: int,
    hops: list[dict[str, Any]],
    origin_addresses: set[str],
    tx_hash: str = "",
    network: str = "",
) -> dict:
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
        entropy = -sum((c / n_total) * math.log2(c / n_total) for c in addr_counts.values())
        max_entropy = math.log2(n_unique)
        entropy = entropy / max_entropy if max_entropy > 0 else 0.0
    else:
        entropy = 0.0

    # Round amount flag: origin amount is a round number (divisible by 1 ADA)
    round_amount_flag = origin_amount > 0 and origin_amount % LOVELACE_PER_ADA == 0

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
        deltas = [
            hop_slots[i + 1] - hop_slots[i]
            for i in range(len(hop_slots) - 1)
            if hop_slots[i + 1] > hop_slots[i]
        ]
        mean_delta = sum(deltas) / len(deltas) if deltas else float(_DEFAULT_INTER_HOP_DELTA_SLOTS)
    else:
        mean_delta = float(_DEFAULT_INTER_HOP_DELTA_SLOTS)

    return {
        "cycle_length": cycle_length,
        "addresses": list(set(addresses))[:20],
        "hops": hops,
        "amount_similarity": round(amount_similarity, 4),
        "net_loss_ratio": round(net_loss_ratio, 4),
        # Deterministic origin representative: set iteration order is unstable
        # across processes (string hash randomization), so list(set)[0] made
        # the recurrence query target and the origin_cluster key vary run-to-run
        # (a cycle could score differently on re-score after a rollback). Use
        # the same _first_sorted representative the hop machinery already uses.
        "recurrence_count": _count_origin_recurrence(
            _first_sorted(origin_addresses),
            network,
            tx_hash,
        ),
        "recipient_entropy": round(entropy, 4),
        "round_amount_flag": round_amount_flag,
        "temporal_concentration": round(min(temporal_concentration, 1.0), 4),
        "mean_inter_hop_delta_slots": round(mean_delta, 2),
        "origin_cluster": _first_sorted(origin_addresses) or "__unknown__",
    }
