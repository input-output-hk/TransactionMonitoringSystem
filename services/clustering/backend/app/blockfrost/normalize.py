"""Pure functions mapping Blockfrost JSON into ClickHouse row dataclasses.

Kept side-effect free so they can be unit tested against captured fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models import AssetRecord, TxRecord, UtxoRecord

# Which inputs were ACTUALLY consumed depends on the script outcome:
#  - succeeded (valid_contract=true): the regular inputs; collateral inputs are
#    returned untouched and reference inputs are read-only.
#  - failed (valid_contract=false): ONLY the collateral inputs are consumed; the
#    regular inputs stay unspent on-chain.
# Features and the co-spend graph model the consumed set, which keeps exactly the
# failed txs an anomaly detector should see clearly faithful: the collateral
# spender is the entity that authorized (and paid for) the failed attempt.


def _lovelace(amount: list[dict[str, Any]]) -> int:
    for entry in amount:
        if entry.get("unit") == "lovelace":
            return int(entry.get("quantity", 0))
    return 0


def _non_lovelace(amount: list[dict[str, Any]]) -> list[tuple[str, int]]:
    # Use .get throughout: a single malformed amount entry (missing unit or
    # quantity) must not raise and abort the whole ingest run.
    return [
        (unit, int(entry.get("quantity", 0)))
        for entry in amount
        if (unit := entry.get("unit")) and unit != "lovelace"
    ]


def _is_real_input(io: dict[str, Any], *, valid_contract: bool) -> bool:
    """Was this input consumed on-chain? Reference inputs never are; collateral
    inputs are consumed exactly when the script FAILED (and then the regular
    inputs are not). See the module comment above."""
    if io.get("reference"):
        return False
    is_collateral = bool(io.get("collateral"))
    return is_collateral != valid_contract  # failed → collateral; succeeded → regular


def _is_real_output(io: dict[str, Any]) -> bool:
    """Exclude the collateral-return output (present only on a script-failed
    tx). Counting it as a normal output would inflate output volume/count on
    exactly the invalid transactions an anomaly detector should flag correctly."""
    return not io.get("collateral")


def _input_sort_key(io: dict[str, Any]) -> tuple[str, int]:
    """Stable on-chain identity of a consumed UTXO: (source tx_hash, output_index).
    Sorting inputs by this makes the per-input ``idx`` deterministic across
    re-fetches, so re-ingesting a tx can't produce un-dedupable duplicate rows
    (the ``tx_utxos`` ORDER BY includes ``idx``)."""
    return (str(io.get("tx_hash", "")), int(io.get("output_index", 0)))


def _collect_io(
    target: str, tx_hash: str, role: str, idx: int, io: dict[str, Any]
) -> tuple[UtxoRecord, list[AssetRecord]]:
    """Map one UTXO into its UtxoRecord and any native-asset AssetRecords."""
    amount = io.get("amount", [])
    utxo = UtxoRecord(
        target=target,
        tx_hash=tx_hash,
        role=role,
        idx=idx,
        address=io.get("address", ""),
        lovelace=_lovelace(amount),
    )
    assets = [
        AssetRecord(target, tx_hash, role, idx, unit, qty) for unit, qty in _non_lovelace(amount)
    ]
    return utxo, assets


def build_records(
    target: str,
    target_type: str,
    tx_detail: dict[str, Any],
    utxos: dict[str, Any],
) -> tuple[TxRecord, list[UtxoRecord], list[AssetRecord]]:
    """Build the transaction, UTXO and asset rows for a single transaction.

    `tx_detail` is the `/txs/{hash}` payload; `utxos` is `/txs/{hash}/utxos`.
    """
    tx_hash = tx_detail["hash"]
    valid_contract = bool(tx_detail.get("valid_contract", True))

    # Sort inputs by their consumed-UTXO identity for a stable, re-fetch-invariant
    # idx (see _input_sort_key). Outputs already carry a real on-chain output_index.
    inputs = sorted(
        (io for io in utxos.get("inputs", []) if _is_real_input(io, valid_contract=valid_contract)),
        key=_input_sort_key,
    )
    outputs = [io for io in utxos.get("outputs", []) if _is_real_output(io)]

    utxo_rows: list[UtxoRecord] = []
    asset_rows: list[AssetRecord] = []
    units: set[str] = set()

    # Inputs are keyed by position; outputs by their on-chain output_index.
    io_items: list[tuple[str, int, dict[str, Any]]] = [
        ("input", pos, io) for pos, io in enumerate(inputs)
    ]
    io_items += [("output", int(io.get("output_index", 0)), io) for io in outputs]

    for role, idx, io in io_items:
        utxo, assets = _collect_io(target, tx_hash, role, idx, io)
        utxo_rows.append(utxo)
        asset_rows.extend(assets)
        units.update(a.unit for a in assets)

    tx = TxRecord(
        target=target,
        target_type=target_type,
        tx_hash=tx_hash,
        block_height=int(tx_detail.get("block_height") or 0),
        block_time=datetime.fromtimestamp(int(tx_detail.get("block_time", 0)), tz=UTC),
        slot=int(tx_detail.get("slot") or 0),
        fees=int(tx_detail.get("fees") or 0),
        deposit=int(tx_detail.get("deposit") or 0),
        size=int(tx_detail.get("size") or 0),
        valid_contract=int(valid_contract),
        input_count=len(inputs),
        output_count=len(outputs),
        total_input_lovelace=sum(r.lovelace for r in utxo_rows if r.role == "input"),
        total_output_lovelace=sum(r.lovelace for r in utxo_rows if r.role == "output"),
        distinct_input_addresses=len({r.address for r in utxo_rows if r.role == "input"}),
        distinct_output_addresses=len({r.address for r in utxo_rows if r.role == "output"}),
        distinct_assets=len(units),
        redeemer_count=int(tx_detail.get("redeemer_count") or 0),
    )
    return tx, utxo_rows, asset_rows
