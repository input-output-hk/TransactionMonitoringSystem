"""Dataclasses mirroring the ClickHouse row shapes.

Field names match the ClickHouse column names exactly so the storage layer can
build insert rows generically with `getattr`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class TxRecord:
    target: str
    target_type: str  # 'address' | 'policy'
    tx_hash: str
    block_height: int
    block_time: datetime
    slot: int
    fees: int
    deposit: int
    size: int
    valid_contract: int
    input_count: int
    output_count: int
    total_input_lovelace: int
    total_output_lovelace: int
    distinct_input_addresses: int
    distinct_output_addresses: int
    distinct_assets: int
    redeemer_count: int


@dataclass(slots=True)
class UtxoRecord:
    target: str
    tx_hash: str
    role: str  # 'input' | 'output'
    idx: int
    address: str
    lovelace: int


@dataclass(slots=True)
class AssetRecord:
    target: str
    tx_hash: str
    role: str
    idx: int
    unit: str
    quantity: int
