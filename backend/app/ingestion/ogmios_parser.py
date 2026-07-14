"""Parser for Ogmios v6 transaction format into NormalizedTransaction"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional
import logging

from app.analysis.features import (
    extract_fee,
    extract_lovelace,
    flatten_assets,
    total_withdrawal_lovelace,
)
from app.models.transaction import (
    NormalizedTransaction,
    TransactionInput,
    TransactionOutput,
)

logger = logging.getLogger(__name__)


def ogmios_input_ref(inp: Dict[str, Any]) -> tuple[str, int]:
    """Extract ``(tx_hash, output_index)`` from an Ogmios input reference.

    Tolerates both shapes the node emits: v6 nests the source tx as
    ``{"transaction": {"id": "<hash>"}}`` while v5 / some payloads inline it as
    ``{"transaction": "<hash>"}``. A missing index defaults to 0.
    """
    inp_tx = inp.get("transaction", {})
    tx_hash = inp_tx.get("id", "") if isinstance(inp_tx, dict) else str(inp_tx)
    return tx_hash, inp.get("index", 0)


def parse_ogmios_transaction(
    tx_data: Dict[str, Any],
    block_slot: Optional[int] = None,
    block_hash: Optional[str] = None,
    block_height: Optional[int] = None,
    timestamp: Optional[datetime] = None,
    block_index: Optional[int] = None,
) -> NormalizedTransaction:
    """Parse an Ogmios v6 transaction into NormalizedTransaction.

    Ogmios v6 transaction structure (Babbage era):
    {
        "id": "tx-hash-hex",
        "fee": {"lovelace": 200000},
        "inputs": [{"transaction": {"id": "..."}, "index": 0}],
        "outputs": [{"address": "addr...", "value": {"lovelace": 1000000, "policyId.assetName": 1}}],
        "metadata": {"labels": {"674": {"json": ...}}},
        ...
    }
    """
    tx_hash = tx_data.get("id", "")

    # Phase-2 validation marker (Ogmios v6): "spends" is "inputs" when the
    # transaction validated and "collaterals" when a Plutus script failed
    # phase-2 validation. For a failed tx the ledger consumes the COLLATERAL
    # inputs and creates only the collateralReturn output; the regular
    # inputs stay live and the regular outputs never exist on-chain.
    # Absent (mempool txs, v5 payloads) means "validated".
    script_valid = tx_data.get("spends", "inputs") != "collaterals"

    # Fee: v5/v6 shape handling lives in features.extract_fee.
    fee = extract_fee(tx_data)

    # Parse inputs.
    # Ogmios does not include resolved UTxO values in the transaction body, so
    # input amounts cannot be determined here.  total_input_value is left as
    # None (unknown) rather than 0 (known zero) to avoid misleading analytics.
    # Regular inputs are ALWAYS emitted first (indices 0..k aligned with
    # raw_data["inputs"], which the enrichment patcher keys on). For a failed
    # tx they carry is_unspent_attempt=1: the ledger did NOT consume them
    # (the collaterals below carry the consumption), but what a failed
    # attack TRIED to spend is high-value signal — dropping them made failed
    # attempts invisible to contention queries (review finding). Flow and
    # displacement readers exclude the flag.
    inputs = []
    for inp in tx_data.get("inputs", []):
        inp_tx_hash, inp_index = ogmios_input_ref(inp)
        # Ogmios inputs don't include resolved address/value in the transaction body;
        # address and amount are only available if resolved via UTxO queries.
        inputs.append(
            TransactionInput(
                tx_hash=inp_tx_hash,
                index=inp_index,
                address="",  # not resolved in Ogmios tx body
                amount=0,
                is_unspent_attempt=not script_valid,
            )
        )

    # Parse reference inputs
    for inp in tx_data.get("references", []):
        inp_tx_hash, inp_index = ogmios_input_ref(inp)
        inputs.append(
            TransactionInput(
                tx_hash=inp_tx_hash,
                index=inp_index,
                address="",
                amount=0,
                is_reference=True,
            )
        )

    # Parse collateral inputs. For a validated tx these were NOT consumed
    # (recorded with the is_collateral flag so analytics can exclude them);
    # for a failed tx they are exactly what the ledger consumed.
    for inp in tx_data.get("collaterals", []):
        inp_tx_hash, inp_index = ogmios_input_ref(inp)
        inputs.append(
            TransactionInput(
                tx_hash=inp_tx_hash,
                index=inp_index,
                address="",
                amount=0,
                is_collateral=True,
            )
        )

    # Parse outputs. A failed tx's regular outputs never exist on-chain, so
    # they are skipped (raw_data retains them); only the collateralReturn is
    # created. Conversely a validated tx never creates the collateralReturn.
    outputs = []
    total_output_value = 0
    addresses = set()

    if script_valid:
        for out in tx_data.get("outputs", []):
            address = out.get("address", "")
            addresses.add(address)

            value = out.get("value", {})
            # v5/v6 lovelace + flattened "policy.name" asset shape handling
            # lives in features.extract_lovelace / flatten_assets.
            lovelace = extract_lovelace(value)
            assets = flatten_assets(value)
            asset_dict = assets if assets else None

            outputs.append(
                TransactionOutput(
                    address=address,
                    amount=int(lovelace),
                    assets=asset_dict,
                )
            )
            total_output_value += int(lovelace)
    else:
        collateral_return = tx_data.get("collateralReturn")
        if collateral_return:
            addr = collateral_return.get("address", "")
            # v5/v6 shape handling lives in features.extract_lovelace (this
            # was the last hand-rolled copy of the dual-shape branch — the
            # duplication class that caused the v6 mempool fee bug).
            cr_value = collateral_return.get("value", {})
            lv = extract_lovelace(cr_value)
            # Native assets returned with the collateral: for a failed tx
            # this is the ONLY output, so dropping them hid every asset a
            # failed attack posted as collateral (Ticket F).
            cr_assets = flatten_assets(cr_value)
            addresses.add(addr)
            outputs.append(
                TransactionOutput(
                    address=addr,
                    amount=int(lv),
                    assets=cr_assets if cr_assets else None,
                    is_collateral=True,
                    # Babbage rule: the collateral return's on-chain index is
                    # the count of regular outputs (which never materialise for
                    # a failed tx but still occupy the index space). Storing it
                    # at enumerate-position 0 made any later spend of this UTxO
                    # unresolvable (review finding).
                    output_index=len(tx_data.get("outputs", [])),
                )
            )
            total_output_value += int(lv)

    # Add input addresses (mostly empty for Ogmios)
    for inp in inputs:
        if inp.address:
            addresses.add(inp.address)

    # Reward-account withdrawals: the stake addresses are involved parties
    # (a drained reward account is prime attack signal). Recorded for a
    # failed tx too: the ledger never applied its withdrawal, but what a
    # failed attack TRIED to withdraw is signal, mirroring the
    # is_unspent_attempt treatment of its regular inputs. The withdrawn
    # value is stamped as withdrawal_total (raw, ungated); the enrichment
    # folds it into total_input_value for validated txs only.
    withdrawals_raw = tx_data.get("withdrawals")
    if isinstance(withdrawals_raw, dict):
        for reward_addr in withdrawals_raw:
            if reward_addr:
                addresses.add(str(reward_addr))
    withdrawal_total = total_withdrawal_lovelace(tx_data)

    # Metadata
    metadata = None
    meta_raw = tx_data.get("metadata")
    if meta_raw:
        labels = meta_raw.get("labels", meta_raw) if isinstance(meta_raw, dict) else {}
        if labels:
            metadata = {}
            for label, content in labels.items():
                if isinstance(content, dict):
                    metadata[label] = content.get("json", content)
                else:
                    metadata[label] = content

    # Deposit (v6 nests as {"ada": {"lovelace": N}}; v5 as {"lovelace": N}).
    # None stays None ("no deposit field"), distinct from a known 0.
    deposit = tx_data.get("deposit")
    if deposit is not None:
        deposit = extract_lovelace(deposit)

    # input_count / output_count are CONSUMED / CREATED counts: regular
    # inputs and outputs for a validated tx, collaterals and the
    # collateralReturn for a failed one. Reference inputs and (for valid
    # txs) collateral inputs are recorded in `inputs` with their flags but
    # never counted; previously they inflated input_count on every Plutus tx.
    # Derived from the model's consumed_by_ledger predicate, the same rule
    # the enrichment uses for total_input_value, so count and value can
    # never disagree on what the ledger consumed.
    input_count = sum(1 for i in inputs if i.consumed_by_ledger(script_valid))

    return NormalizedTransaction(
        tx_hash=tx_hash,
        slot=block_slot,
        block_height=block_height,
        block_hash=block_hash,
        block_index=block_index,
        timestamp=timestamp or datetime.now(timezone.utc),
        fee=int(fee),
        deposit=deposit,
        inputs=inputs,
        outputs=outputs,
        input_count=input_count,
        output_count=len(outputs),
        total_input_value=None,
        total_output_value=total_output_value,
        withdrawal_total=withdrawal_total,
        addresses=list(addresses),
        metadata=metadata,
        script_valid=script_valid,
        raw_data=tx_data,
    )
