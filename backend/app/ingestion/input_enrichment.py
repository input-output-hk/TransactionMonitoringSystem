"""UTxO input enrichment for ingested transactions.

Ogmios delivers transaction inputs as bare (tx_hash, index) references; the
functions here resolve them to addresses and lovelace amounts so scorers and
the address screen see real values. Three sources, in priority order: the
mempool-time ledger query (parse_resolved_utxo, cached until the tx confirms),
outputs of earlier transactions in the same block, and a ClickHouse batch
lookup for everything else (resolve_input_amounts).

Both public entry points route through one per-tx core (_resolve_tx_inputs)
so the mempool-cache path and the block-batch path cannot drift: the
consumption rule lives on the model (TransactionInput.consumed_by_ledger)
and the withdrawal fold reads the parser-stamped tx.withdrawal_total.

Pure functions over NormalizedTransaction: no instance state, no ordering
constraints. The caller (OgmiosClient) owns when enrichment is applied and
when the mempool cache is consumed; those orderings are durability-critical
and live with the chain-sync persistence logic, not here.
"""

import logging
from typing import Any

from app.analysis.features import extract_lovelace, flatten_assets
from app.db import clickhouse
from app.models.transaction import NormalizedTransaction, TransactionInput

logger = logging.getLogger(__name__)

# lookup value shape shared by both resolution paths:
# (address, amount_lovelace, assets or None to keep the input's own)
ResolvedRef = tuple[str, int, dict[str, int] | None]


def _withdrawal_total(tx: NormalizedTransaction) -> int:
    """Reward-account withdrawals fold into total_input_value: withdrawn
    rewards fund outputs exactly like spent inputs. Only for validated
    txs; a phase-2 failure never applies the withdrawal. The amount is
    stamped by the parser (tx.withdrawal_total), never re-derived here."""
    return tx.withdrawal_total if tx.script_valid else 0


def _flow_addresses(script_valid: bool, inputs: list[TransactionInput]) -> set[str]:
    """Input addresses surfaced to the tx's address list: regular inputs
    always (a failed tx's attempted inputs are attack-attempt signal);
    collateral only for a failed tx, where the collateral payer is the
    consumed party. Reference inputs are read-only, never involved."""
    return {
        i.address
        for i in inputs
        if i.address and not i.is_reference and (not i.is_collateral or not script_valid)
    }


def _resolve_tx_inputs(
    tx: NormalizedTransaction,
    lookup: dict[tuple, ResolvedRef],
) -> tuple[list[TransactionInput], int, bool]:
    """Shared per-tx core of both resolution paths.

    Resolves every non-reference input present in ``lookup`` via
    model_copy, which preserves the flags: spelling the fields out is the
    copy-the-flags bug class that silently dropped is_collateral once
    already. Accumulates the consumed total, seeded with the withdrawal
    fold; only inputs the ledger actually consumed
    (TransactionInput.consumed_by_ledger) feed it, the rest resolve for
    address visibility behind their flags.

    Returns (new_inputs, total, changed); changed is True when at least
    one input newly resolved.
    """
    total = _withdrawal_total(tx)
    new_inputs: list[TransactionInput] = []
    changed = False
    for inp in tx.inputs:
        if inp.is_reference:
            new_inputs.append(inp)
            continue  # read-only: never resolved, never a value flow
        if inp.amount > 0:
            # Already resolved (e.g. mempool cache on a replay).
            if inp.consumed_by_ledger(tx.script_valid):
                total += inp.amount
            new_inputs.append(inp)
            continue
        resolved = lookup.get((inp.tx_hash, inp.index))
        if resolved:
            addr, amt, assets = resolved
            inp = inp.model_copy(
                update={
                    "address": addr,
                    "amount": int(amt),
                    "assets": assets if assets is not None else inp.assets,
                }
            )
            changed = True
            if inp.consumed_by_ledger(tx.script_valid):
                total += int(amt)
        new_inputs.append(inp)
    return new_inputs, total, changed


def parse_resolved_utxo(utxo: dict[str, Any]) -> tuple:
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
    resolved: dict[tuple, dict],
) -> NormalizedTransaction:
    """Enrich a NormalizedTransaction with previously resolved UTxO input data.

    Attempted inputs of a failed tx ARE resolved (their addresses are
    attack-attempt signal and belong in the address screen) but never
    feed total_input_value: the ledger did not consume them. Collateral
    inputs resolve the same way; their amounts count only for a failed
    tx, where they are exactly what the ledger consumed. Reward-account
    withdrawals fold into the total for validated txs.
    """
    lookup: dict[tuple, ResolvedRef] = {
        ref: (u["address"], u["amount"], u.get("assets")) for ref, u in resolved.items()
    }
    new_inputs, total, _ = _resolve_tx_inputs(tx, lookup)
    return tx.model_copy(
        update={
            "inputs": new_inputs,
            "total_input_value": total if total > 0 else None,
            "addresses": list(set(tx.addresses) | _flow_addresses(tx.script_valid, new_inputs)),
        }
    )


async def resolve_input_amounts(
    txs: list[NormalizedTransaction], network: str
) -> list[NormalizedTransaction]:
    """Resolve input addresses and amounts from ClickHouse and intra-block outputs.

    1. Build an intra-block output map from earlier txs in this block.
    2. Collect the unresolved (input_tx_hash, input_index) refs worth a
       cross-block lookup.
    3. Batch-fetch from ClickHouse for cross-block refs.
    4. Apply resolved values to each input via the shared per-tx core.
    """
    # Build intra-block output map: {(tx_hash, output_index): (address, amount)}.
    # Collateral returns included at their EXPLICIT on-chain index (the
    # regular-output count, Babbage): they are real spendable UTxOs and
    # a same-block spend of one must resolve.
    intra_block: dict[tuple, tuple] = {}
    for tx in txs:
        for idx, out in enumerate(tx.outputs):
            chain_idx = out.output_index if out.output_index is not None else idx
            intra_block[(tx.tx_hash, chain_idx)] = (out.address, out.amount)

    # Collect unresolved input refs (skip already-resolved from the mempool
    # cache). Collateral refs go cross-block only for FAILED txs, where they
    # are the consumed flow; a validated tx's collateral feeds neither
    # totals, addresses, nor any detection query, so it is not worth
    # growing this checkpoint-blocking per-block lookup (it still resolves
    # for free when its source sits in the same block). Reference inputs
    # stay unresolved (read-only, never a value flow).
    cross_block_refs = []
    for tx in txs:
        for inp in tx.inputs:
            if inp.is_reference:
                continue
            if inp.is_collateral and tx.script_valid:
                continue
            if inp.amount > 0:
                continue  # already resolved
            ref = (inp.tx_hash, inp.index)
            if ref not in intra_block:
                cross_block_refs.append(ref)

    # Batch fetch from ClickHouse
    ch_resolved: dict[tuple, tuple] = {}
    if cross_block_refs:
        ch_resolved = await clickhouse.get_outputs_for_refs_async(cross_block_refs, network)

    # Merge: intra-block takes priority over ClickHouse. assets=None keeps
    # each input's own (parser-fresh inputs carry none).
    lookup: dict[tuple, ResolvedRef] = {
        ref: (addr, amt, None) for ref, (addr, amt) in {**ch_resolved, **intra_block}.items()
    }

    result = []
    for tx in txs:
        new_inputs, total, changed = _resolve_tx_inputs(tx, lookup)
        # A withdrawal alone updates the total even when no input resolved:
        # withdrawn rewards are consumed value the tx provably moved (the
        # stored value is a lower bound; see the model field description).
        if changed or _withdrawal_total(tx) > 0:
            tx = tx.model_copy(
                update={
                    "inputs": new_inputs,
                    "total_input_value": total if total > 0 else None,
                    "addresses": list(
                        set(tx.addresses) | _flow_addresses(tx.script_valid, new_inputs)
                    ),
                }
            )
        result.append(tx)
    return result
