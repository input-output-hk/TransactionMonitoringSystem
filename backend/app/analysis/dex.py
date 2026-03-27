"""Simplified sandwich pattern detection (structural only).

Detects three-transaction patterns at the same script address within a slot
window, where two of the three transactions share an address cluster (same
first input address).  Does not parse DEX redeemers or compute swap rates.

This is a Phase 4 simplified implementation; swap_rate_delta and price_impact
are set to 0 (structural detection only).
"""

import logging
from typing import Any, Dict, List, Optional

from app.db import clickhouse

logger = logging.getLogger(__name__)

_SLOT_WINDOW = 5

# Cardano script address Bech32 prefixes (Shelley era type bytes 0x11, 0x31, etc.)
_SCRIPT_ADDR_PREFIXES = ("addr1w", "addr1z", "addr_test1w", "addr_test1z")


def _is_script_address(addr: str) -> bool:
    """Check if a Cardano address is a script address by Bech32 prefix."""
    return addr.startswith(_SCRIPT_ADDR_PREFIXES)


def _count_attacker_history(
    client: Any,
    attacker_addr: str,
    network: str,
    current_slot: int,
) -> int:
    """Count how many prior txs from this address cluster appeared as the
    front leg of a potential sandwich (2+ txs at the same script address
    within a slot window, before the current slot)."""
    rows = client.execute(
        """
        SELECT count(DISTINCT i.tx_hash)
        FROM transaction_inputs i
        JOIN transaction_outputs o ON i.tx_hash = o.tx_hash AND i.network = o.network
        WHERE i.network = %(network)s
          AND i.address = %(addr)s
          AND i.input_index = 0
          AND i.is_collateral = 0
          AND i.is_reference = 0
          AND o.is_collateral = 0
          AND o.address LIKE %(script_prefix)s
          AND i.tx_hash IN (
              SELECT tx_hash FROM transactions
              WHERE network = %(network)s AND slot < %(slot)s
          )
        """,
        {
            "network": network,
            "addr": attacker_addr,
            "slot": current_slot,
            "script_prefix": "addr1w%" if not network.startswith("pre") else "addr_test1w%",
        },
    )
    return rows[0][0] if rows else 0


def detect_sandwich_pattern(
    tx_hash: str,
    network: str,
    slot: int,
) -> Optional[Dict]:
    """Check if tx_hash is the victim in a structural sandwich pattern.

    Looks for 3+ txs within _SLOT_WINDOW slots interacting with the same
    script address, where two share a first-input address cluster.
    """
    if not slot:
        return None

    client = clickhouse._get_client()

    # Get script addresses this tx interacts with (via outputs)
    script_rows = client.execute(
        """
        SELECT DISTINCT address
        FROM transaction_outputs
        WHERE tx_hash = %(tx_hash)s
          AND network = %(network)s
          AND is_collateral = 0
        """,
        {"tx_hash": tx_hash, "network": network},
    )
    if not script_rows:
        return None

    addresses = [r[0] for r in script_rows if _is_script_address(r[0])]
    if not addresses:
        return None

    # Find other txs in the slot window that share any of these addresses
    neighbor_rows = client.execute(
        """
        SELECT DISTINCT o.tx_hash, t.slot, t.fee
        FROM transaction_outputs o
        JOIN transactions t ON o.tx_hash = t.tx_hash AND o.network = t.network
        WHERE o.network = %(network)s
          AND o.address IN %(addresses)s
          AND t.slot BETWEEN %(min_slot)s AND %(max_slot)s
          AND o.is_collateral = 0
        ORDER BY t.slot ASC
        LIMIT 20
        """,
        {
            "network": network,
            "addresses": addresses,
            "min_slot": slot - _SLOT_WINDOW,
            "max_slot": slot + _SLOT_WINDOW,
        },
    )

    if len(neighbor_rows) < 3:
        return None

    # Get first input address for each neighbor tx (address cluster proxy)
    neighbor_hashes = [r[0] for r in neighbor_rows]
    input_rows = client.execute(
        """
        SELECT tx_hash, address
        FROM transaction_inputs
        WHERE tx_hash IN %(hashes)s
          AND network = %(network)s
          AND is_collateral = 0
          AND is_reference = 0
          AND address != ''
          AND input_index = 0
        """,
        {"hashes": neighbor_hashes, "network": network},
    )
    first_input_addr = {r[0]: r[1] for r in input_rows}

    # Group by first input address to find linked tx pairs
    addr_to_txs: Dict[str, List[str]] = {}
    for h, addr in first_input_addr.items():
        if h != tx_hash:  # exclude the potential victim
            addr_to_txs.setdefault(addr, []).append(h)

    # Look for an address cluster with 2+ txs (potential attacker front+back)
    victim_addr = first_input_addr.get(tx_hash, "")
    for cluster_addr, cluster_txs in addr_to_txs.items():
        if len(cluster_txs) >= 2 and cluster_addr != victim_addr:
            # Found a structural sandwich pattern
            tx_slots = {r[0]: r[1] for r in neighbor_rows}
            tx_fees = {r[0]: r[2] for r in neighbor_rows}

            # Sort attacker txs by slot to identify front and back
            cluster_txs.sort(key=lambda h: tx_slots.get(h, 0))
            tx_a = cluster_txs[0]  # front
            tx_b = cluster_txs[-1]  # back

            slot_span = abs(tx_slots.get(tx_b, slot) - tx_slots.get(tx_a, slot))

            # Historical attacker recurrence: count prior sandwich-like
            # patterns from the same first-input address cluster.
            hist_count = _count_attacker_history(client, cluster_addr, network, slot)

            return {
                "tx_a": tx_a,
                "tx_b": tx_b,
                "pool_id": addresses[0] if addresses else "",
                "asset_pair": "unknown",
                "attacker_linked": True,
                "swap_rate_victim": 0.0,
                "swap_rate_baseline": 0.0,
                "price_impact_a": 0.0,
                "profit_b": 0.0,
                "attacker_sandwich_count": hist_count,
                "slot_span": slot_span,
            }

    return None
