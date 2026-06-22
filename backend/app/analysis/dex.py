"""Simplified sandwich pattern detection (structural only).

Detects three-transaction patterns at the same script address within a slot
window, where two of the three transactions share an address cluster (same
first input address).  Does not parse DEX redeemers or compute swap rates.

This is a Phase 4 simplified implementation; swap_rate_delta and price_impact
are set to 0 (structural detection only).
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.analysis.scorer_config import get as _get_cfg
from app.analysis.features import SCRIPT_ADDRESS_PREFIXES, is_script_address
from app.db import clickhouse

logger = logging.getLogger(__name__)

# Slot window for sandwich pattern detection. Read from the sandwich scorer
# config so a single edit in detection.yaml propagates to both the detector
# and the scorer (previously hardcoded here AND defined in detection.yaml as
# sandwich.window_slots, allowing silent drift).
_SLOT_WINDOW = int(_get_cfg("sandwich")["window_slots"])
# Cap on neighbour rows fetched around a victim tx. Bounds the per-tx query
# cost; a busier window than this is batcher traffic, not a 3-leg sandwich.
_NEIGHBOR_LIMIT = int(_get_cfg("sandwich")["neighbor_limit"])

# Single source of truth for script-address prefixes: features.py owns the
# CIP-19 enumeration so the detector and the scorers cannot drift.
_SCRIPT_ADDR_PREFIXES = SCRIPT_ADDRESS_PREFIXES


def _is_script_address(addr: str) -> bool:
    """Check if a Cardano address is a script address by Bech32 prefix.

    Delegates to the canonical features.is_script_address (the single CIP-19
    source) so this detector cannot fork from the scorers' definition.
    """
    return is_script_address(addr)


def _network_script_prefixes(network: str) -> List[str]:
    """The script prefixes valid for ``network`` (testnet variants for
    preprod/preview, mainnet variants otherwise)."""
    is_testnet = network.startswith("pre")
    return [p for p in _SCRIPT_ADDR_PREFIXES if ("_test" in p) == is_testnet]


def _count_attacker_history(
    client: Any,
    attacker_addr: str,
    network: str,
    current_slot: int,
) -> int:
    """Count how many prior txs from this address cluster appeared as the
    front leg of a potential sandwich (2+ txs at the same script address
    within a slot window, before the current slot)."""
    prefixes = _network_script_prefixes(network)
    like_clause = " OR ".join(
        f"o.address LIKE %(script_prefix_{i})s" for i in range(len(prefixes))
    )
    params: Dict[str, Any] = {
        "network": network,
        "addr": attacker_addr,
        "slot": current_slot,
    }
    for i, prefix in enumerate(prefixes):
        params[f"script_prefix_{i}"] = f"{prefix}%"
    rows = client.execute(
        f"""
        SELECT count(DISTINCT i.tx_hash)
        FROM transaction_inputs i
        JOIN transaction_outputs o ON i.tx_hash = o.tx_hash AND i.network = o.network
        WHERE i.network = %(network)s
          AND i.address = %(addr)s
          AND i.input_index = 0
          AND i.is_collateral = 0
          AND i.is_reference = 0
          AND i.is_unspent_attempt = 0
          AND o.is_collateral = 0
          AND ({like_clause})
          AND i.tx_hash IN (
              SELECT tx_hash FROM transactions
              WHERE network = %(network)s AND slot < %(slot)s
          )
        """,
        params,
    )
    return rows[0][0] if rows else 0


def _attacker_net_ada(
    client: Any,
    attacker_addr: str,
    leg_hashes: List[str],
    network: str,
) -> int:
    """Net lovelace the attacker wallet gained across the front + back legs.

    ``profit = sum(attacker outputs) - sum(attacker spent inputs)`` over the
    two attacker legs. A real sandwich round-trips a position and ends with
    more ADA than it put in; a coincidental structural triple nets <= 0.
    Collateral and reference inputs are excluded. Computed generically from
    the ingested input/output amounts, no DEX redeemer or pool-datum parsing.
    Token-denominated profit is not captured (ADA-only); see the blind-spot
    note in detection.yaml (sandwich.min_profit_lovelace).

    The intermediate UTxO the attacker returns to itself in the front leg is
    spent again in the back leg, so it appears in both the outputs and the
    inputs sums and cancels: the net therefore reflects only externally gained
    ADA, not the round-tripped position. Input value is resolved from the
    referenced output (``coalesce(o.amount, ti.amount)``) because a minority of
    ``transaction_inputs`` rows carry an unresolved ``amount`` of 0; falling
    back to the join keeps an unresolved input from understating cost and
    overstating profit.
    """
    # FINAL on every summed table: this is the sandwich economic gate, so a
    # not-yet-merged ReplacingMergeTree duplicate would inflate "profit" and
    # fabricate confirmations. (FINAL inside a join must be a subquery.)
    out_rows = client.execute(
        """
        SELECT sum(amount) FROM transaction_outputs FINAL
        WHERE tx_hash IN %(hashes)s AND network = %(network)s
          AND address = %(addr)s AND is_collateral = 0
        """,
        {"hashes": leg_hashes, "network": network, "addr": attacker_addr},
    )
    in_rows = client.execute(
        """
        SELECT sum(coalesce(o.amount, ti.amount))
        FROM (
            SELECT tx_hash, network, address, amount,
                   input_tx_hash, input_index_in_tx
            FROM transaction_inputs FINAL
            WHERE tx_hash IN %(hashes)s AND network = %(network)s
              AND address = %(addr)s AND is_collateral = 0 AND is_reference = 0
              AND is_unspent_attempt = 0
        ) ti
        LEFT JOIN (
            -- Parent-UTxO resolution: no is_collateral filter here, a failed
            -- tx's collateral return is a real spendable UTxO (Babbage).
            SELECT tx_hash, network, output_index, amount
            FROM transaction_outputs FINAL
            WHERE network = %(network)s
              AND tx_hash IN (
                  SELECT input_tx_hash FROM transaction_inputs FINAL
                  WHERE tx_hash IN %(hashes)s AND network = %(network)s
                    AND address = %(addr)s
                    AND is_collateral = 0 AND is_reference = 0
                    AND is_unspent_attempt = 0
              )
        ) o
          ON o.tx_hash = ti.input_tx_hash
         AND o.output_index = ti.input_index_in_tx
         AND o.network = ti.network
        """,
        {"hashes": leg_hashes, "network": network, "addr": attacker_addr},
    )
    out_amt = int(out_rows[0][0] or 0) if out_rows else 0
    in_amt = int(in_rows[0][0] or 0) if in_rows else 0
    return out_amt - in_amt


def _pool_script_addresses(client: Any, tx_hash: str, network: str) -> List[str]:
    """Script addresses this tx pays into: the candidate pool/venue addresses."""
    rows = client.execute(
        """
        SELECT DISTINCT address
        FROM transaction_outputs
        WHERE tx_hash = %(tx_hash)s
          AND network = %(network)s
          AND is_collateral = 0
        """,
        {"tx_hash": tx_hash, "network": network},
    )
    return [r[0] for r in rows if _is_script_address(r[0])]


def _neighbors_in_window(client: Any, addresses: List[str], network: str, slot: int):
    """Txs paying into the same pool addresses within +/- _SLOT_WINDOW slots.

    Returns rows of (tx_hash, slot, block_index, fee) ordered by
    (slot, block_index), capped at ``sandwich.neighbor_limit``. block_index is
    the tx's position within its block, giving intra-block ordering so a
    same-slot front/victim/back can be sequenced; coalesced to 0 for rows
    ingested before the column existed.
    """
    return client.execute(
        """
        SELECT DISTINCT o.tx_hash, t.slot, coalesce(t.block_index, 0) AS block_index, t.fee
        FROM transaction_outputs o
        JOIN transactions t ON o.tx_hash = t.tx_hash AND o.network = t.network
        WHERE o.network = %(network)s
          AND o.address IN %(addresses)s
          AND t.slot BETWEEN %(min_slot)s AND %(max_slot)s
          AND o.is_collateral = 0
        ORDER BY t.slot ASC, block_index ASC
        LIMIT %(neighbor_limit)s
        """,
        {
            "network": network,
            "addresses": addresses,
            "min_slot": slot - _SLOT_WINDOW,
            "max_slot": slot + _SLOT_WINDOW,
            "neighbor_limit": _NEIGHBOR_LIMIT,
        },
    )


def _first_input_addresses(
    client: Any, tx_hashes: List[str], network: str,
) -> Dict[str, str]:
    """Map each tx to its first-input address (the address-cluster proxy)."""
    rows = client.execute(
        """
        SELECT tx_hash, address
        FROM transaction_inputs
        WHERE tx_hash IN %(hashes)s
          AND network = %(network)s
          AND is_collateral = 0
          AND is_reference = 0
          AND is_unspent_attempt = 0
          AND address != ''
          AND input_index = 0
        """,
        {"hashes": tx_hashes, "network": network},
    )
    return {r[0]: r[1] for r in rows}


def _tx_position(client: Any, tx_hash: str, network: str):
    """The ``(slot, block_index)`` ordering key for a tx, or None if unknown.

    Used to place the victim relative to the attacker legs when the victim falls
    outside the capped neighbour window. block_index is coalesced to 0 (matching
    _neighbors_in_window) for rows ingested before the column existed.
    """
    rows = client.execute(
        "SELECT slot, coalesce(block_index, 0) FROM transactions "
        "WHERE tx_hash = %(h)s AND network = %(n)s LIMIT 1",
        {"h": tx_hash, "n": network},
    )
    if rows and rows[0][0] is not None:
        return (int(rows[0][0]), int(rows[0][1]))
    return None


def _bracketing_legs(
    cluster_txs: List[str],
    pos: Dict[str, Tuple[int, int]],
    victim_pos: Tuple[int, int],
) -> Optional[Tuple[str, str, int]]:
    """Closest attacker leg before the victim and after it, by (slot, block_index).

    Returns ``(tx_a, tx_b, slot_span)`` when the cluster's legs straddle the
    victim (front before, back after) -- the defining sandwich shape -- or None
    when they don't (co-occurrence, not a sandwich). ``pos`` maps each neighbour
    tx_hash to its ``(slot, block_index)`` ordering key.

    Caveat: legacy rows predating the block_index column carry block_index 0
    (coalesced upstream), so a same-slot group of such rows shares one position
    and cannot be ordered -- those same-slot sandwiches stay unconfirmable (as
    before block_index existed), rather than being mis-bracketed.
    """
    cluster_pos = [(pos[h], h) for h in cluster_txs if h in pos]
    before = sorted(p for p in cluster_pos if p[0] < victim_pos)
    after = sorted(p for p in cluster_pos if p[0] > victim_pos)
    if not before or not after:
        return None
    front_pos, tx_a = before[-1]   # last leg before the victim
    back_pos, tx_b = after[0]      # first leg after the victim
    return tx_a, tx_b, abs(back_pos[0] - front_pos[0])


def detect_sandwich_pattern(
    tx_hash: str,
    network: str,
    slot: int,
) -> Optional[Dict]:
    """Check if tx_hash is the victim in a structural sandwich pattern.

    Looks for 3+ txs within _SLOT_WINDOW slots interacting with the same script
    address, where a wallet cluster has at least one leg ordered BEFORE the
    victim and one AFTER it (temporal bracketing by ``(slot, block_index)``) --
    the defining front-run/back-run shape of a sandwich.
    """
    if not slot:
        return None

    client = clickhouse._get_client()

    # Script addresses this tx pays into (the candidate pools/venues).
    addresses = _pool_script_addresses(client, tx_hash, network)
    if not addresses:
        return None

    # Other txs sharing those addresses within the slot window.
    neighbor_rows = _neighbors_in_window(client, addresses, network, slot)
    if len(neighbor_rows) < 3:
        return None

    # First-input address per neighbor tx (the address-cluster proxy).
    neighbor_hashes = [r[0] for r in neighbor_rows]
    first_input_addr = _first_input_addresses(client, neighbor_hashes, network)

    # Group by first input address to find linked tx pairs
    addr_to_txs: Dict[str, List[str]] = {}
    for h, addr in first_input_addr.items():
        if h != tx_hash:  # exclude the potential victim
            addr_to_txs.setdefault(addr, []).append(h)

    # Look for an address cluster with 2+ txs (potential attacker front+back)
    victim_addr = first_input_addr.get(tx_hash, "")
    for cluster_addr, cluster_txs in addr_to_txs.items():
        if len(cluster_txs) < 2 or cluster_addr == victim_addr:
            continue
        # A sandwich attacker controls a wallet (payment key). A script-address
        # cluster is the pool/batcher venue moving its own funds, which is the
        # dominant structural false positive on eUTxO DEXs (the batcher nets
        # ADA from fees, not from sandwiching). Skip it and keep looking for a
        # genuine wallet attacker. Blind spot: a script-based sandwich bot.
        if _is_script_address(cluster_addr):
            continue

        # Temporal bracketing: a sandwich front-runs BEFORE the victim and
        # back-runs AFTER it, ordered by (slot, block_index) so the sequence is
        # established even within a single block. Co-occurring legs that don't
        # straddle the victim are not a sandwich (the dominant arbitrage/batcher
        # false positive).
        pos = {r[0]: (r[1], r[2]) for r in neighbor_rows}
        victim_pos = pos.get(tx_hash) or _tx_position(client, tx_hash, network)
        if victim_pos is None:
            continue
        legs = _bracketing_legs(cluster_txs, pos, victim_pos)
        if legs is None:
            continue  # legs do not bracket the victim -> not a sandwich
        tx_a, tx_b, slot_span = legs

        # Historical attacker recurrence: count prior sandwich-like
        # patterns from the same first-input address cluster.
        hist_count = _count_attacker_history(client, cluster_addr, network, slot)

        # Economic confirmation: attacker net ADA across the two legs. The
        # scorer suppresses the candidate entirely when this is below the
        # configured profit floor (a zero-profit triple is not a sandwich).
        profit = _attacker_net_ada(client, cluster_addr, [tx_a, tx_b], network)

        return {
            "tx_a": tx_a,
            "tx_b": tx_b,
            "pool_id": addresses[0] if addresses else "",
            "asset_pair": "unknown",
            "attacker_linked": True,
            "swap_rate_victim": 0.0,
            "swap_rate_baseline": 0.0,
            "price_impact_a": 0.0,
            "profit_b": float(profit),
            "attacker_sandwich_count": hist_count,
            "slot_span": slot_span,
        }

    return None
