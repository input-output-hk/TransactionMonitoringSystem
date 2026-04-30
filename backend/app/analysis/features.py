"""Extended feature extraction from raw Ogmios transaction data.

Extracts UTxO-level and transaction-level features beyond what the ingestion
parser captures: CBOR byte sizes, datum information, redeemer counts, execution
units, and minting details.  These features feed the 9 attack-class scoring
pipelines defined in the Polimi detection spec.

All functions accept a raw_data dict (the original Ogmios JSON payload,
already stored per transaction) and return structured tuples ready for
ClickHouse insertion.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy-load cbor2 to avoid import errors when the package is not installed
# (e.g. during lightweight testing of unrelated modules).
_cbor2 = None


def _get_cbor2():
    global _cbor2
    if _cbor2 is None:
        import cbor2
        _cbor2 = cbor2
    return _cbor2


# ---------------------------------------------------------------------------
# Address classification
# ---------------------------------------------------------------------------

def has_spend_redeemer(raw_data: Dict[str, Any]) -> bool:
    """True if the tx has at least one spend-purpose redeemer (Plutus input).

    Native scripts never carry redeemers: they are declarative ledger predicates
    (signatures, timelocks, n-of-m clauses) evaluated without Plutus execution.
    By contract (Cardano ledger: ``dom txrdmrs ≡ᵉ scriptRdrptrs``), every
    Plutus-script input must have exactly one ``spend`` redeemer; therefore
    "no spend redeemer" implies "all script inputs are native".

    This is the structural test the ``multiple_sat`` gate uses to skip
    native-script multisig wallets, which are immune to multiple-satisfaction
    by construction (no validator code to short-circuit).

    Caveat: a True return does not localise the Plutus inputs to a specific
    script address. In a mixed tx (Plutus mint or spend on script A combined
    with native-script consolidation on script B), this returns True even
    though script B's inputs are still native. Callers needing per-script
    granularity must inspect redeemer pointers (``spend:N`` keys or
    ``validator.index`` fields) and map them to input indices.
    """
    redeemers = raw_data.get("redeemers")
    if not redeemers:
        return False
    if isinstance(redeemers, dict):
        # Ogmios v6 keys are "spend:N" / "mint:N" / "publish:N" / "withdraw:N".
        return any(str(k).startswith("spend") for k in redeemers.keys())
    if isinstance(redeemers, list):
        for r in redeemers:
            if not isinstance(r, dict):
                continue
            tag = r.get("validator")
            if isinstance(tag, dict) and tag.get("purpose") == "spend":
                return True
            if r.get("purpose") == "spend":
                return True
    return False


def is_script_address(address: str) -> bool:
    """Detect whether a Cardano address is a script (validator) address.

    Cardano Shelley-era addresses encode the payment credential type in the
    header nibble.  For Bech32 addresses the human-readable prefix indicates:
      - addr1q / addr_test1q  -> payment key (normal wallet)
      - addr1w / addr_test1w  -> script payment credential (validator)
      - addr1z / addr_test1z  -> script payment + staking key
    Enterprise and pointer addresses follow similar patterns.

    This heuristic covers the vast majority of addresses seen on Preprod and
    Mainnet.  Byron-era addresses (Ae2...) are never script addresses.
    """
    if not address:
        return False
    laddr = address.lower()
    # Script payment credential prefixes (mainnet + testnet variants)
    return (
        laddr.startswith("addr1w")
        or laddr.startswith("addr_test1w")
        or laddr.startswith("addr1z")
        or laddr.startswith("addr_test1z")
    )


# ---------------------------------------------------------------------------
# UTxO-level feature extraction
# ---------------------------------------------------------------------------

def _estimate_value_cbor_bytes(value: Dict[str, Any]) -> int:
    """Estimate the CBOR byte size of an Ogmios output value dict.

    Ogmios represents the value as {"lovelace": N, "policyId": {"assetName": qty}}.
    We re-encode a simplified structure to CBOR to approximate the on-chain size.
    """
    try:
        cbor2 = _get_cbor2()
        # Build a structure similar to the on-chain CBOR encoding:
        # - If only lovelace: just an integer
        # - If multi-asset: (lovelace, {policy_bytes: {asset_bytes: qty}})
        # Handle both Ogmios v5 {"lovelace": N, ...} and v6 {"ada": {"lovelace": N}, ...}
        ada_obj = value.get("ada")
        if isinstance(ada_obj, dict):
            lovelace = ada_obj.get("lovelace", 0)
        else:
            lovelace = value.get("lovelace", 0)
        assets = {}
        for key, val in value.items():
            if key in ("lovelace", "ada"):
                continue
            if isinstance(val, dict):
                assets[key.encode()] = {
                    aname.encode(): int(qty) for aname, qty in val.items()
                }
            else:
                assets[key.encode()] = int(val)

        if not assets:
            return len(cbor2.dumps(int(lovelace)))
        return len(cbor2.dumps([int(lovelace), assets]))
    except Exception:
        # Fallback: rough estimate based on JSON size
        return len(json.dumps(value).encode())



def _nonzero_qty(q: Any) -> bool:
    """Return True if the asset quantity is non-zero (positive or negative)."""
    try:
        return int(q) != 0
    except (TypeError, ValueError):
        return False


def count_assets(value: Dict[str, Any]) -> Tuple[int, int]:
    """Count unique policy IDs and live asset classes in a value dict.

    Zero-quantity entries are skipped: they appear in mint/burn payloads
    where a policy is listed with qty=0 (burn-only). Counting them inflates
    ``unique_token_count`` and causes false positives in token_dust scoring
    on wallet sweeps of previously held assets.

    Returns ``(unique_policy_count, unique_token_count)``.
    """
    policies = set()
    token_count = 0
    for key, val in value.items():
        if key in ("lovelace", "ada"):
            continue
        if isinstance(val, dict):
            live = sum(1 for q in val.values() if _nonzero_qty(q))
            if live:
                policies.add(key)
                token_count += live
        elif _nonzero_qty(val):
            policies.add(key)
            token_count += 1
    return len(policies), token_count


def _extract_datum_info(output: Dict[str, Any]) -> Tuple[int, int]:
    """Extract datum presence flag and byte size from an Ogmios output.

    Returns (datum_present, datum_bytes).
    datum_present: 0=none, 1=datum_hash, 2=inline_datum
    """
    # Inline datum (Babbage era)
    datum = output.get("datum")
    if datum is not None:
        if isinstance(datum, str):
            # Hex-encoded CBOR datum
            return 2, len(datum) // 2
        if isinstance(datum, dict):
            encoded = json.dumps(datum).encode()
            return 2, len(encoded)
        return 2, 0

    # Datum hash only
    datum_hash = output.get("datumHash")
    if datum_hash:
        return 1, 0  # hash present but datum bytes unknown without indexer

    return 0, 0


def extract_utxo_features(
    tx_hash: str,
    network: str,
    raw_data: Dict[str, Any],
) -> List[tuple]:
    """Extract UTxO-level features from raw Ogmios transaction data.

    Returns a list of tuples ready for insert_utxo_features().
    """
    outputs = raw_data.get("outputs", [])
    rows = []
    for idx, out in enumerate(outputs):
        address = out.get("address", "")
        value = out.get("value", {})
        if not isinstance(value, dict):
            value = {"lovelace": int(value) if value else 0}

        ada_obj = value.get("ada")
        if isinstance(ada_obj, dict):
            ada_amount = ada_obj.get("lovelace", 0)
        else:
            ada_amount = value.get("lovelace", 0)
        value_cbor = _estimate_value_cbor_bytes(value)
        policy_count, token_count = count_assets(value)
        datum_flag, datum_bytes = _extract_datum_info(out)

        # Estimate total UTxO bytes (address + value + datum + script ref)
        address_bytes = len(address.encode()) if address else 0
        script_ref = out.get("script")
        script_bytes = len(json.dumps(script_ref).encode()) if script_ref else 0
        utxo_total = address_bytes + value_cbor + datum_bytes + script_bytes

        datum_ratio = datum_bytes / (utxo_total + 1e-6) if utxo_total > 0 else 0.0

        rows.append((
            tx_hash,
            network,
            idx,
            address,
            1 if is_script_address(address) else 0,
            int(ada_amount),
            value_cbor,
            policy_count,
            token_count,
            datum_flag,
            datum_bytes,
            round(datum_ratio, 4),
            utxo_total,
        ))
    return rows


# ---------------------------------------------------------------------------
# Transaction-level script feature extraction
# ---------------------------------------------------------------------------

def extract_tx_script_features(
    tx_hash: str,
    network: str,
    raw_data: Dict[str, Any],
) -> Optional[tuple]:
    """Extract transaction-level script execution features.

    Returns a single tuple ready for insert_tx_script_features(),
    or None if no script features are present (simple payment tx).
    """
    redeemers = raw_data.get("redeemers")
    mint = raw_data.get("mint")

    # Count spending inputs (non-reference, non-collateral)
    inputs = raw_data.get("inputs", [])
    spending_inputs = len(inputs)

    redeemers_count = 0
    exunits_mem = 0
    exunits_cpu = 0

    if redeemers:
        if isinstance(redeemers, list):
            redeemers_count = len(redeemers)
            for r in redeemers:
                budget = r.get("executionUnits", r.get("budget", {}))
                exunits_mem += int(budget.get("memory", 0))
                exunits_cpu += int(budget.get("cpu", budget.get("steps", 0)))
        elif isinstance(redeemers, dict):
            # Ogmios v6 may use dict keyed by "spend:N", "mint:N", etc.
            redeemers_count = len(redeemers)
            for key, r in redeemers.items():
                budget = r.get("executionUnits", r.get("budget", {}))
                exunits_mem += int(budget.get("memory", 0))
                exunits_cpu += int(budget.get("cpu", budget.get("steps", 0)))

    mint_policy_count = 0
    mint_entries_json = ""
    if mint:
        if isinstance(mint, dict):
            mint_policy_count = len(mint)
            entries = []
            for policy_id, assets in mint.items():
                if isinstance(assets, dict):
                    for asset_name, qty in assets.items():
                        entries.append({
                            "policy_id": policy_id,
                            "asset_name": asset_name,
                            "quantity": int(qty),
                        })
            mint_entries_json = json.dumps(entries) if entries else ""

    # Only store a row if there's script activity worth recording
    if redeemers_count == 0 and mint_policy_count == 0:
        return None

    return (
        tx_hash,
        network,
        redeemers_count,
        spending_inputs,
        exunits_mem,
        exunits_cpu,
        mint_policy_count,
        mint_entries_json,
    )


# ---------------------------------------------------------------------------
# Convenience: extract all features for a batch
# ---------------------------------------------------------------------------

def extract_all_features(
    tx_hash: str,
    network: str,
    raw_data: Dict[str, Any],
) -> Tuple[List[tuple], Optional[tuple]]:
    """Extract both UTxO-level and tx-level features from raw data.

    Returns (utxo_feature_rows, tx_script_feature_row).
    """
    utxo_rows = extract_utxo_features(tx_hash, network, raw_data)
    script_row = extract_tx_script_features(tx_hash, network, raw_data)
    return utxo_rows, script_row
