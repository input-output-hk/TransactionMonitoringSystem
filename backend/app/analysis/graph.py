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

from app.analysis.features import LOVELACE_PER_ADA, is_script_address
from app.analysis.normalise import BAND_HIGH_THRESHOLD
from app.analysis.scorer_config import anchor as _anchor
from app.analysis.scorer_config import get as _get_cfg
from app.config import settings
from app.db import clickhouse
from app.utils.bech32 import payment_credential_or_raw

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
# The gate's length bounds. A closure outside them is never scored, so neither
# its closing leg nor its history is worth a read (see _closing_leg_amounts and
# _cycle_history).
_MIN_CYCLE_LENGTH = int(_CYCLE_CFG["min_length"])
_MAX_CYCLE_LENGTH = int(_CYCLE_CFG["max_length"])
# Outputs to the origin set up to which the closing leg's similarity reading
# tries every subset of them (2^N sums); beyond it, see _closing_reading.
_CLOSING_SUBSET_MAX_OUTPUTS = int(_CYCLE_CFG["closing_subset_max_outputs"])
_RECURRENCE_WINDOW_DAYS = int(_CIRCULAR_CFG["recurrence_window_days"])
# Every Shelley-era Cardano network has a one-second slot (genesis
# slotLength = 1), so a day of chain time is this many slots.
_SLOTS_PER_DAY = 86_400
# The history window in chain time, so a re-score reads the same earlier cycles
# the live score did, and never a later one.
_RECURRENCE_WINDOW_SLOTS = _RECURRENCE_WINDOW_DAYS * _SLOTS_PER_DAY
# The amount similarity above which the circular scorer's amount axis starts to
# score (its p50 anchor): a cycle below it does not pass value along.
_VALUE_PRESERVING_SIMILARITY = float(_anchor(_CIRCULAR_CFG["fixed_anchors"], "amount_sim")[0])
# Two hops in one block land in the same slot. A slot is the chain's time
# resolution, so they are at most one slot apart: measured, and as fast as a
# ring can move, not unmeasurable.
_SAME_SLOT_DELTA_SLOTS = 1


def _address_key(address: str) -> str:
    """The identity an address is compared by across an origin's cycles.

    A key-hash payment credential names the wallet that controls the address,
    whatever stake part the address carries, so that credential is the key. A
    script address stays whole: one script hash is shared by every user of a
    DEX or a lending pool, and keying on it would merge all of them.
    """
    if is_script_address(address):
        return address
    return payment_credential_or_raw(address)


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
    # For the ring's path (see _ring_path): the transaction that first paid
    # each address the search reached, and the frontier queried at each hop.
    creator: dict[str, str] = dict.fromkeys(current_addresses, tx_hash)
    queried: list[list[str]] = []

    for hop in range(1, max_hops + 1):
        if not current_addresses:
            break

        # Deterministic frontier: set iteration order varies across Python
        # processes (string hash randomization), so an unsorted truncation
        # would explore a different address subset run-to-run and silently
        # miss cycles through the dropped legs. Same rationale as
        # _first_sorted, applied to the cap that actually controls recall.
        addr_list = sorted(current_addresses)[:max_fanout]
        queried.append(addr_list)

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
        # closes the cycle; _closing_leg_amounts then reads that transaction's
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
                # Cycle found. This row identifies the closing transaction; how
                # much it returned comes from all of that transaction's outputs
                # to the origin set, not this one row (see _closing_leg_amounts).
                returned, closing_amount, closing_leg_unavailable = _closing_leg_amounts(
                    client,
                    network,
                    r[0],
                    hop + 1,
                    origin_addresses,
                    out_amt,
                    [h["amount_lovelace"] for h in hops],
                )
                all_cycle_addresses.extend(list(current_addresses))
                all_cycle_addresses.append(out_addr)
                hops.append({"address": out_addr, "amount_lovelace": closing_amount, "slot": slot})
                intermediaries, prior_cycles, history_unavailable = _cycle_history(
                    client,
                    network,
                    origin_tx=tx_hash,
                    closing_tx=r[0],
                    cycle_length=hop + 1,
                    origin_addresses=origin_addresses,
                    hops=hops,
                    queried=queried,
                    creator=creator,
                )

                return _build_cycle_result(
                    cycle_length=hop + 1,
                    addresses=all_cycle_addresses,
                    origin_amount=origin_amount,
                    final_amount=returned,
                    hops=hops,
                    origin_addresses=origin_addresses,
                    intermediaries=intermediaries,
                    prior_cycles=prior_cycles,
                    history_unavailable=history_unavailable,
                    closing_leg_unavailable=closing_leg_unavailable,
                )

            if out_addr not in visited_addresses:
                next_addresses.add(out_addr)
                creator.setdefault(out_addr, r[0])
                hop_amount += out_amt

        all_cycle_addresses.extend(list(current_addresses))
        hop_repr = _first_sorted(current_addresses)
        hops.append({"address": hop_repr, "amount_lovelace": hop_amount, "slot": hop_slot})
        visited_addresses |= next_addresses
        current_addresses = next_addresses

    return None


def _closing_leg_amounts(
    client: Any,
    network: str,
    closing_tx: str,
    cycle_length: int,
    origin_addresses: set[str],
    matched_amount: int,
    prior_hop_amounts: list[int],
) -> tuple[int, int, bool]:
    """What the closing transaction returned: (total, amount for similarity, read failed).

    The hop query answers whether a cycle closes, not how much came back. Its
    rows are DISTINCT on (tx_hash, address, amount, slot) and the loop stops at
    the first origin-paying row, so that row's amount let a closing leg hide its
    repayment two ways: a dust output to one origin address sorted ahead of the
    real repayment to another, or the repayment split into equal outputs that
    DISTINCT collapses into one. Either pushed net_loss_ratio toward 1.0 and
    CircularScorer's hard gate discarded a genuine cycle. So every
    non-collateral output of the closing transaction that pays the origin set is
    read here, without DISTINCT.

    The two amounts answer different questions. The total feeds net_loss_ratio,
    the gate: extra outputs can only lower the measured loss, never raise it.
    The second feeds amount_similarity, which asks whether the same quantity
    passed through every hop, and there the total is the wrong reading whenever
    the closing leg also pays the wallet change or re-locks value at a script
    the origin spent from: the extra would count as a mismatch and push a real
    cycle out of the alert band, and would also deny it the recycling credit,
    which only a cycle that passes value along gets (see _build_cycle_result).
    So it is the part of the payment that fits the other hops best (see
    _closing_reading), of which the matched row and the total are both
    candidates: the similarity never comes out lower than under either earlier
    reading (the matched row before the total existed, the total after).

    FINAL for the same reason origin_amount uses it: a not-yet-merged duplicate
    row would otherwise double the total. A closure the gate never scores skips
    the read. A failed read keeps the cycle, measured with the matched row
    (exact when the closing leg pays the origin a single output), and flags it:
    the engine then retries the transaction, and if the read keeps failing
    writes the score with a marker rather than dropping the cycle.
    """
    if not _MIN_CYCLE_LENGTH <= cycle_length <= _MAX_CYCLE_LENGTH:
        return matched_amount, matched_amount, False
    try:
        rows = client.execute(
            """
            SELECT address, amount
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
            "Closing-leg read failed for %s; measuring the cycle with the matched output",
            closing_tx[:16],
            exc_info=True,
        )
        return matched_amount, matched_amount, True
    paid = [(address, int(amount)) for address, amount in rows]
    # The matched output is one of the summed rows, so the total cannot be
    # smaller unless the two reads disagree; never report less than was seen.
    total = max(sum(amount for _, amount in paid), matched_amount)
    return total, _closing_reading(paid, matched_amount, total, prior_hop_amounts), False


def _closing_reading(
    paid: list[tuple[str, int]],
    matched_amount: int,
    total: int,
    prior_hop_amounts: list[int],
) -> int:
    """The part of the closing leg's payment to the origin that fits the other hops best.

    Change or a re-lock can sit beside the repayment on the same address or
    another, and the repayment itself can be split across outputs, so the
    candidates are the sums of every subset of the outputs. Past
    circular.cycle.closing_subset_max_outputs outputs that is too many sums,
    and the candidates are each output, each address's total and the total. The
    matched row and the total are always candidates, and ties go to the total.

    amount_similarity is unimodal in the closing amount x: 1 - CV falls on
    either side of x = sum(h^2) / sum(h) over the other hops h, so only the
    candidate nearest that point from below and from above can score highest.
    """
    amounts = [amount for _, amount in paid]
    if len(amounts) <= _CLOSING_SUBSET_MAX_OUTPUTS:
        sums = {0}
        for amount in amounts:
            sums |= {s + amount for s in sums}
        sums.discard(0)
    else:
        per_address: Counter[str] = Counter()
        for address, amount in paid:
            per_address[address] += amount
        sums = {*amounts, *per_address.values()}
    sums |= {matched_amount, total}
    # Total first, so max() keeps it on a tie.
    finalists = [total]
    weight = sum(prior_hop_amounts)
    if weight > 0:
        peak = sum(h * h for h in prior_hop_amounts) / weight
        below = max((c for c in sums if c <= peak), default=None)
        above = min((c for c in sums if c >= peak), default=None)
        finalists += [c for c in (below, above) if c is not None]
    else:
        finalists += sorted(sums)
    return max(finalists, key=lambda c: _amount_similarity([*prior_hop_amounts, c]))


def _cycle_history(
    client: Any,
    network: str,
    *,
    origin_tx: str,
    closing_tx: str,
    cycle_length: int,
    origin_addresses: set[str],
    hops: list[dict[str, Any]],
    queried: list[list[str]],
    creator: dict[str, str],
) -> tuple[list[str], list[tuple[float, list[str]]], bool]:
    """The ring's path and its origin's earlier cycles, for the window axes.

    Returns ``(intermediaries, prior cycles, whether the read failed)``. A
    closure outside the gate's length bounds is never scored, so it gets its
    per-step representatives and no read: most closures are two-hop round trips
    through a script, and the read would otherwise run, and could fail, for
    every one of them.

    A failed read does not raise. The cycle keeps its representatives and no
    history, which scores it as it scored before the window existed, and is
    flagged so enrichment asks the engine to retry the transaction. A read
    that keeps failing then writes that score with a marker rather than losing
    the finding.
    """
    representatives = _representatives(hops)
    if not _MIN_CYCLE_LENGTH <= cycle_length <= _MAX_CYCLE_LENGTH:
        return representatives, [], False
    try:
        path = _ring_path(client, network, origin_tx, closing_tx, queried, creator)
        prior_cycles = _prior_origin_cycles(
            client, network, origin_addresses, int(hops[0]["slot"]), origin_tx
        )
    except Exception:
        logger.warning(
            "Circular history read failed for %s; scoring the cycle without it",
            origin_tx[:16],
            exc_info=True,
        )
        return representatives, [], True
    # An empty path means transaction_inputs lacks a spend the hop query saw;
    # the representatives are the closest measure left.
    return path or representatives, prior_cycles, False


def _ring_path(
    client: Any,
    network: str,
    origin_tx: str,
    closing_tx: str,
    queried: list[list[str]],
    creator: dict[str, str],
) -> list[str]:
    """Every address the ring's value passed through between the origin and the close.

    The search expands whole frontiers and learns only which transaction
    closes the cycle, so the path is recovered backwards: the addresses of each
    frontier that spent into the transactions found one level up, then the
    transactions that first paid those addresses, down to the origin
    transaction. Recipients that never carried value back are left out, and the
    address that pays the origin back is included. One small primary-key read
    per level, and only for a closure the gate can score.
    """
    on_path: set[str] = set()
    txs = {closing_tx}
    for frontier in reversed(queried):
        if not txs:
            break
        spenders = _spenders(client, network, txs) & set(frontier)
        on_path |= spenders
        txs = {creator[a] for a in spenders} - {origin_tx}
    return sorted(on_path)


def _spenders(client: Any, network: str, txs: set[str]) -> set[str]:
    """Addresses the given transactions spent from, as the hop query counts a spend.

    FINAL so an outdated version of an input row cannot add an address, with
    skip indexes off: under FINAL they re-select every granule sharing a key
    range with the selection (see _prior_origin_cycles).
    """
    rows = client.execute(
        """
        SELECT DISTINCT address
        FROM transaction_inputs FINAL
        WHERE network = %(network)s
          AND tx_hash IN %(txs)s
          AND is_collateral = 0
          AND is_reference = 0
          AND is_unspent_attempt = 0
        SETTINGS use_skip_indexes_if_final = 0
        """,
        {"network": network, "txs": sorted(txs)},
    )
    return {r[0] for r in rows}


def _prior_origin_cycles(
    client: Any,
    network: str,
    origin_addresses: set[str],
    origin_slot: int,
    exclude_tx: str,
) -> list[tuple[float, list[str]]]:
    """Earlier cycles from the same origin in the recurrence window.

    Returns ``(circular score, intermediaries)`` per earlier cycle, read from
    what scoring persisted, so no cycle is re-walked. A cycle is from the same
    origin when one of its origin addresses matches one of this cycle's,
    compared by _address_key; rows stored before the origin set was recorded
    carry only their representative address, so raw addresses match too.
    Scored cycles are read and so are the ones the scorer suppressed as
    structural-only, which store their path as well; a -1 row without
    intermediaries has nothing to compare (the scorer never engaged it, or
    suppressed it before paths were stored). The window is
    chain time, circular.recurrence_window_days up to this cycle's first slot,
    so a re-score reads what the live score read and never a later cycle.

    The columns are materialized from the stored evidence
    (clickhouse_schema._CIRCULAR_HISTORY_COLUMNS) and read for the whole
    network under FINAL. That cost follows the table, not the origin: on a
    mainnet-shaped synthetic table (1.53M scores in 7 parts, 6.3M inputs) every
    origin took 0.26-0.30 CPU-s and 150-180 MiB per call, where the recurrence
    query this replaced took 0.25 s for a new origin and 1.3-1.6 s for one that
    spent thousands of times, the kind that makes most production calls. Skip
    indexes are off because under FINAL they re-select every granule sharing
    a key range with the selection, which one small fresh part makes the whole
    table; none serves these predicates anyway, so the setting only keeps a
    future index from changing that.
    """
    keys = {_address_key(a) for a in origin_addresses} | set(origin_addresses)
    rows = client.execute(
        """
        SELECT circular, circular_intermediaries
        FROM tx_class_scores FINAL
        WHERE network = %(network)s
          AND hasAny(circular_origins, %(origins)s)
          AND (circular >= 0 OR notEmpty(circular_intermediaries))
          AND circular_first_slot >= %(min_slot)s
          AND circular_first_slot <= %(max_slot)s
          AND tx_hash != %(exclude)s
        SETTINGS use_skip_indexes_if_final = 0
        """,
        {
            "network": network,
            "origins": sorted(keys),
            "min_slot": max(0, origin_slot - _RECURRENCE_WINDOW_SLOTS),
            "max_slot": origin_slot,
            "exclude": exclude_tx,
        },
    )
    # circular >= 0 is the schema convention for "scored"; -1 is no finding.
    return [(float(score), list(intermediaries)) for score, intermediaries in rows]


def _representatives(hops: list[dict[str, Any]]) -> list[str]:
    """Each step's representative address, without the origin and the close.

    What a cycle's intermediaries fall back to when its path is not read, and
    what rows stored before the path was recorded hold.
    """
    return [h["address"] for h in hops[1:-1]]


def _shannon_bits(observations: list[str]) -> float:
    """Shannon entropy, in bits, of how often each distinct value occurs."""
    counts = Counter(observations)
    n = len(observations)
    # p * log2(1/p) rather than -(p * log2(p)): the same sum, but a single
    # repeated value then comes out 0.0 instead of -0.0 in the stored evidence.
    return sum((c / n) * math.log2(n / c) for c in counts.values())


def _amount_similarity(hop_amounts: list[int]) -> float:
    """1 - CV(hop_amounts), clipped to [0, 1]: how evenly value passed through."""
    if len(hop_amounts) >= 2:
        mean_amt = statistics.mean(hop_amounts)
        if mean_amt > 0:
            cv = statistics.stdev(hop_amounts) / mean_amt
            return max(0.0, min(1.0, 1.0 - cv))
        return 0.0
    if hop_amounts and hop_amounts[0] > 0:
        return 1.0
    return 0.0


def _build_cycle_result(
    cycle_length: int,
    addresses: list[str],
    origin_amount: int,
    final_amount: int,
    hops: list[dict[str, Any]],
    origin_addresses: set[str],
    intermediaries: list[str],
    prior_cycles: list[tuple[float, list[str]]],
    history_unavailable: bool = False,
    closing_leg_unavailable: bool = False,
) -> dict:
    """Build the cycle dict expected by the CircularScorer.

    ``hops`` is the single source of truth for per-step amounts and slots;
    we derive ``hop_amounts`` / ``hop_slots`` from it for the stats math
    below. ``addresses`` is kept as a separate parameter because the
    entropy calculation needs the full per-step address list (which may
    include duplicates and is longer than ``hops`` when a step had
    multiple recipients), not just the per-hop representative.
    ``intermediaries`` and ``prior_cycles`` come from _cycle_history: this
    cycle's path and its origin's earlier cycles as (circular score,
    intermediaries) pairs, which the recurrence and recycling axes are
    measured over.
    """
    hop_amounts = [int(h.get("amount_lovelace", 0)) for h in hops]
    hop_slots = [int(h.get("slot", 0)) for h in hops]

    amount_similarity = _amount_similarity(hop_amounts)

    # Net loss ratio: how much value was lost (fees)
    if origin_amount > 0:
        net_loss_ratio = max(0, origin_amount - final_amount) / origin_amount
    else:
        net_loss_ratio = 1.0

    # Recipient entropy: Shannon entropy of address frequency distribution
    n_unique = len(set(addresses))
    if n_unique > 1:
        cycle_entropy = _shannon_bits(addresses) / math.log2(n_unique)
    else:
        cycle_entropy = 0.0

    # Recycling across the origin's window. A cycle is credited only for its
    # own reuse: the share of its intermediaries that the origin's earlier
    # cycles already passed through. Earlier cycles through other addresses
    # can then neither dilute a ring that reuses its intermediaries nor lend
    # their concentration to one that reuses none.
    current_keys = {_address_key(a) for a in intermediaries}
    prior = [(score, {_address_key(a) for a in path}) for score, path in prior_cycles]
    seen: set[str] = set().union(*(keys for _, keys in prior))
    recycled_share = len(current_keys & seen) / len(current_keys) if current_keys else 0.0
    # On the entropy axis's scale, reusing every intermediary is fully
    # concentrated and reusing none fully diverse. The lower of this and the
    # cycle's own reading is kept, so the window can only raise the sub-score.
    # Credited only to a cycle that passes value along (the similarity the
    # scorer sees, above the amount axis's p50 anchor): bots that route
    # unrelated amounts through the same hubs otherwise read as recycled rings:
    # replaying every stored mainnet cycle, 732 of 4,584, none passing value
    # along, would have moved into Moderate without this, and none could reach
    # High either way.
    if round(amount_similarity, 4) > _VALUE_PRESERVING_SIMILARITY:
        entropy = min(cycle_entropy, 1.0 - recycled_share)
    else:
        entropy = cycle_entropy

    # Earlier cycles that reached High and went through at least one of the
    # same intermediaries: "the same cycle or a near-identical one" (Polimi
    # Section 4.7.2). High only, so a low score cannot boost every later cycle
    # from the origin, which would then count in turn (Section 4.7.3).
    recurrence_count = sum(
        1 for score, keys in prior if score >= BAND_HIGH_THRESHOLD and keys & current_keys
    )

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

    # Mean inter-hop delta in slots. Hops in one block share a slot and count
    # as _SAME_SLOT_DELTA_SLOTS apart; dropping them fell back to the default
    # and scored a ring chained within one block, the fastest there is, as
    # slow. Only a later hop at an earlier slot is left out.
    if len(hop_slots) >= 2:
        deltas = [
            max(hop_slots[i + 1] - hop_slots[i], _SAME_SLOT_DELTA_SLOTS)
            for i in range(len(hop_slots) - 1)
            if hop_slots[i + 1] >= hop_slots[i]
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
        "recurrence_count": recurrence_count,
        "prior_cycles_in_window": len(prior_cycles),
        "recycled_share": round(recycled_share, 4),
        "recipient_entropy": round(entropy, 4),
        "round_amount_flag": round_amount_flag,
        "temporal_concentration": round(min(temporal_concentration, 1.0), 4),
        "mean_inter_hop_delta_slots": round(mean_delta, 2),
        "origin_cluster": _first_sorted(origin_addresses) or "__unknown__",
        # Stored with the score, so the origin's later cycles can read them back
        # (clickhouse_schema._CIRCULAR_HISTORY_COLUMNS).
        "intermediaries": list(intermediaries),
        "origin_keys": sorted({_address_key(a) for a in origin_addresses}),
        "history_unavailable": history_unavailable,
        # What the closing leg paid the origin set in total, which the loss is
        # measured on. The closing hop's amount above is the part of it that
        # fits the ring (see _closing_reading), so the two differ by any change.
        "returned_lovelace": int(final_amount),
        "closing_leg_unavailable": closing_leg_unavailable,
    }
