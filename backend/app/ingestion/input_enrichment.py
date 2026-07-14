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
from typing import Any, Dict, List, Set

from app.analysis.features import (
    extract_lovelace,
    flatten_assets,
    total_withdrawal_lovelace,
)
from app.db import clickhouse
from app.models.transaction import NormalizedTransaction, TransactionInput

logger = logging.getLogger(__name__)


def _consumes_value(script_valid: bool, inp: TransactionInput) -> bool:
    """Whether the ledger actually consumed this input's value.

    Mirrors the parser's input_count rule: regular inputs for a validated
    tx, collateral inputs for a failed one. Reference inputs are read-only
    and a failed tx's regular inputs (is_unspent_attempt) stayed live.
    """
    if inp.is_reference:
        return False
    if script_valid:
        return not inp.is_collateral and not inp.is_unspent_attempt
    return inp.is_collateral


def _withdrawal_total(tx: NormalizedTransaction) -> int:
    """Reward-account withdrawals fold into total_input_value: withdrawn
    rewards fund outputs exactly like spent inputs. Only for validated
    txs; a phase-2 failure never applies the withdrawal."""
    return total_withdrawal_lovelace(tx.raw_data) if tx.script_valid else 0


def _flow_addresses(script_valid: bool, inputs: List[TransactionInput]) -> Set[str]:
    """Input addresses surfaced to the tx's address list: regular inputs
    always (a failed tx's attempted inputs are attack-attempt signal);
    collateral only for a failed tx, where the collateral payer is the
    consumed party. Reference inputs are read-only, never involved."""
    return {
        i.address for i in inputs
        if i.address and not i.is_reference
        and (not i.is_collateral or not script_valid)
    }


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
    feed total_input_value: the ledger did not consume them. Collateral
    inputs resolve the same way; their amounts count only for a failed
    tx, where they are exactly what the ledger consumed. Reward-account
    withdrawals fold into the total for validated txs.
    """
    total = _withdrawal_total(tx)
    new_inputs = []
    for inp in tx.inputs:
        if not inp.is_reference:
            utxo = resolved.get((inp.tx_hash, inp.index))
            if utxo:
                inp = TransactionInput(
                    tx_hash=inp.tx_hash,
                    index=inp.index,
                    address=utxo["address"],
                    amount=utxo["amount"],
                    assets=utxo.get("assets"),
                    is_reference=False,
                    is_collateral=inp.is_collateral,
                    is_unspent_attempt=inp.is_unspent_attempt,
                )
                if _consumes_value(tx.script_valid, inp):
                    total += utxo["amount"]
        new_inputs.append(inp)

    resolved_addrs = _flow_addresses(tx.script_valid, new_inputs)
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

    # Collect all unresolved input refs (skip already-resolved from mempool
    # cache). Collateral inputs resolve too: for a failed tx they are the
    # consumed flow, and for a validated one the payer address is still
    # involved-party data behind the is_collateral flag. Reference inputs
    # stay unresolved (read-only, never a value flow).
    cross_block_refs = []
    for tx in txs:
        for inp in tx.inputs:
            if inp.is_reference:
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
        withdrawal_total = _withdrawal_total(tx)
        total = withdrawal_total
        new_inputs = []
        changed = False
        for inp in tx.inputs:
            if inp.is_reference:
                new_inputs.append(inp)
                continue  # read-only: never resolved, never a value flow
            if inp.amount > 0:
                if _consumes_value(tx.script_valid, inp):
                    total += inp.amount
                new_inputs.append(inp)
                continue
            ref = (inp.tx_hash, inp.index)
            resolved = all_resolved.get(ref)
            if resolved:
                addr, amt = resolved
                new_inp = TransactionInput(
                    tx_hash=inp.tx_hash,
                    index=inp.index,
                    address=addr,
                    amount=int(amt),
                    assets=inp.assets,
                    is_reference=False,
                    is_collateral=inp.is_collateral,
                    is_unspent_attempt=inp.is_unspent_attempt,
                )
                new_inputs.append(new_inp)
                # Resolution is for address visibility; only inputs the
                # ledger actually consumed feed total_input_value (regular
                # inputs when validated, collateral when failed).
                if _consumes_value(tx.script_valid, new_inp):
                    total += int(amt)
                changed = True
            else:
                new_inputs.append(inp)

        # A withdrawal alone updates the total even when no input resolved:
        # withdrawn rewards are consumed value the tx provably moved.
        if changed or withdrawal_total > 0:
            resolved_addrs = _flow_addresses(tx.script_valid, new_inputs)
            tx = tx.model_copy(update={
                "inputs": new_inputs,
                "total_input_value": total if total > 0 else None,
                "addresses": list(set(tx.addresses) | resolved_addrs),
            })
        result.append(tx)
    return result
