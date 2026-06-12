"""UTxO input enrichment for ingested transactions.

Ogmios delivers transaction inputs as bare (tx_hash, index) references; the
functions here resolve them to addresses and lovelace amounts so scorers and
the address screen see real values. Three sources, in priority order: the
mempool-time ledger query (parse_resolved_utxo, cached until the tx confirms),
outputs of earlier transactions in the same block, and a ClickHouse batch
lookup for everything else (resolve_input_amounts).

Pure functions over NormalizedTransaction: no instance state, no ordering
constraints. The caller (OgmiosClient) owns when enrichment is applied and
when the mempool cache is consumed; those orderings are durability-critical
and live with the chain-sync persistence logic, not here.
"""

import logging
from typing import Any, Dict, List

from app.analysis.features import extract_lovelace, flatten_assets
from app.db import clickhouse
from app.models.transaction import NormalizedTransaction, TransactionInput

logger = logging.getLogger(__name__)


def parse_resolved_utxo(utxo: Dict[str, Any]) -> tuple:
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


def apply_resolved_inputs(
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


async def resolve_input_amounts(
    txs: List[NormalizedTransaction], network: str
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
            cross_block_refs, network
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
