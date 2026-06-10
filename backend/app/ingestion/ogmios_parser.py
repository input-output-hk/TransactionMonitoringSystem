"""Parser for Ogmios v6 transaction format into NormalizedTransaction"""

from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import logging

from app.models.transaction import (
    NormalizedTransaction,
    TransactionInput,
    TransactionOutput,
)

logger = logging.getLogger(__name__)


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

    # Fee
    # Ogmios v6 wraps fee as {"ada": {"lovelace": N}} in Conway/Babbage era
    fee_obj = tx_data.get("fee", {})
    if isinstance(fee_obj, dict):
        ada = fee_obj.get("ada")
        if isinstance(ada, dict):
            fee = ada.get("lovelace", 0)   # {"ada": {"lovelace": N}}
        else:
            fee = fee_obj.get("lovelace", 0)  # legacy {"lovelace": N}
    else:
        fee = int(fee_obj) if fee_obj else 0

    # Parse inputs.
    # Ogmios does not include resolved UTxO values in the transaction body, so
    # input amounts cannot be determined here.  total_input_value is left as
    # None (unknown) rather than 0 (known zero) to avoid misleading analytics.
    inputs = []
    spending_input_count = 0
    if script_valid:
        for inp in tx_data.get("inputs", []):
            inp_tx = inp.get("transaction", {})
            inp_tx_hash = inp_tx.get("id", "") if isinstance(inp_tx, dict) else str(inp_tx)
            inp_index = inp.get("index", 0)
            # Ogmios inputs don't include resolved address/value in the transaction body;
            # address and amount are only available if resolved via UTxO queries.
            inputs.append(TransactionInput(
                tx_hash=inp_tx_hash,
                index=inp_index,
                address="",  # not resolved in Ogmios tx body
                amount=0,
            ))
            spending_input_count += 1
    # When phase-2 validation failed, the regular inputs were NOT spent:
    # they are omitted from the consumed-input list (the full body survives
    # in raw_data) so displacement detection and flow analytics do not treat
    # live UTxOs as consumed. The collaterals below carry the consumption.

    # Parse reference inputs
    for inp in tx_data.get("references", []):
        inp_tx = inp.get("transaction", {})
        inp_tx_hash = inp_tx.get("id", "") if isinstance(inp_tx, dict) else str(inp_tx)
        inputs.append(TransactionInput(
            tx_hash=inp_tx_hash,
            index=inp.get("index", 0),
            address="",
            amount=0,
            is_reference=True,
        ))

    # Parse collateral inputs. For a validated tx these were NOT consumed
    # (recorded with the is_collateral flag so analytics can exclude them);
    # for a failed tx they are exactly what the ledger consumed.
    collateral_count = 0
    for inp in tx_data.get("collaterals", []):
        inp_tx = inp.get("transaction", {})
        inp_tx_hash = inp_tx.get("id", "") if isinstance(inp_tx, dict) else str(inp_tx)
        inputs.append(TransactionInput(
            tx_hash=inp_tx_hash,
            index=inp.get("index", 0),
            address="",
            amount=0,
            is_collateral=True,
        ))
        collateral_count += 1

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
            if isinstance(value, dict):
                # Ogmios v6: {"ada": {"lovelace": N}, ...}
                # Ogmios v5: {"lovelace": N, ...}
                ada = value.get("ada")
                if isinstance(ada, dict):
                    lovelace = ada.get("lovelace", 0)
                else:
                    lovelace = value.get("lovelace", 0)
                # Multi-asset: everything except "lovelace" and "ada" keys
                assets = {}
                for key, val in value.items():
                    if key in ("lovelace", "ada"):
                        continue
                    if isinstance(val, dict):
                        # Format: {"policyId": {"assetName": quantity}}
                        for asset_name, qty in val.items():
                            assets[f"{key}.{asset_name}"] = int(qty)
                    else:
                        assets[key] = int(val)
                asset_dict = assets if assets else None
            else:
                lovelace = int(value) if value else 0
                asset_dict = None

            outputs.append(TransactionOutput(
                address=address,
                amount=int(lovelace),
                assets=asset_dict,
            ))
            total_output_value += int(lovelace)
    else:
        collateral_return = tx_data.get("collateralReturn")
        if collateral_return:
            addr = collateral_return.get("address", "")
            val = collateral_return.get("value", {})
            if isinstance(val, dict):
                ada = val.get("ada")
                lv = ada.get("lovelace", 0) if isinstance(ada, dict) else val.get("lovelace", 0)
            else:
                lv = 0
            addresses.add(addr)
            outputs.append(TransactionOutput(
                address=addr,
                amount=int(lv),
                is_collateral=True,
            ))
            total_output_value += int(lv)

    # Add input addresses (mostly empty for Ogmios)
    for inp in inputs:
        if inp.address:
            addresses.add(inp.address)

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

    # Deposit (v6 nests as {"ada": {"lovelace": N}}; v5 as {"lovelace": N})
    deposit = tx_data.get("deposit")
    if deposit is not None:
        if isinstance(deposit, dict):
            ada = deposit.get("ada")
            if isinstance(ada, dict):
                deposit = ada.get("lovelace", 0)
            else:
                deposit = deposit.get("lovelace", 0)
        deposit = int(deposit)

    # input_count / output_count are CONSUMED / CREATED counts: regular
    # inputs and outputs for a validated tx, collaterals and the
    # collateralReturn for a failed one. Reference inputs and (for valid
    # txs) collateral inputs are recorded in `inputs` with their flags but
    # never counted; previously they inflated input_count on every Plutus tx.
    input_count = spending_input_count if script_valid else collateral_count

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
        addresses=list(addresses),
        metadata=metadata,
        script_valid=script_valid,
        raw_data=tx_data,
    )
