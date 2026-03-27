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

    # Fee
    fee_obj = tx_data.get("fee", {})
    if isinstance(fee_obj, dict):
        fee = fee_obj.get("lovelace", 0)
    else:
        fee = int(fee_obj) if fee_obj else 0

    # Parse inputs.
    # Ogmios does not include resolved UTxO values in the transaction body, so
    # input amounts cannot be determined here.  total_input_value is left as
    # None (unknown) rather than 0 (known zero) to avoid misleading analytics.
    inputs = []
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

    # Parse collateral inputs
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

    # Parse outputs
    outputs = []
    total_output_value = 0
    addresses = set()

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

    # Collateral return output
    collateral_return = tx_data.get("collateralReturn")
    if collateral_return:
        addr = collateral_return.get("address", "")
        val = collateral_return.get("value", {})
        if isinstance(val, dict):
            ada = val.get("ada")
            lv = ada.get("lovelace", 0) if isinstance(ada, dict) else val.get("lovelace", 0)
        else:
            lv = 0
        outputs.append(TransactionOutput(
            address=addr,
            amount=int(lv),
            is_collateral=True,
        ))

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

    # Deposit
    deposit = tx_data.get("deposit")
    if deposit is not None:
        if isinstance(deposit, dict):
            deposit = deposit.get("lovelace", 0)
        deposit = int(deposit)

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
        input_count=len(inputs),
        output_count=len(outputs),
        total_input_value=None,
        total_output_value=total_output_value,
        addresses=list(addresses),
        metadata=metadata,
        raw_data=tx_data,
    )
