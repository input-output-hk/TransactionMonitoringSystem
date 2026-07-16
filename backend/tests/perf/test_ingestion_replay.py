"""Ingestion replay benchmark: Ogmios parse and warehouse insert throughput.

Two measurements judged against the ``ingestion`` section of
``config/performance.yaml``:

- ``parse_tps``: Ogmios v6 JSON -> NormalizedTransaction through
  ``parse_ogmios_transaction``, pure compute with no I/O in the timed region.
- ``insert_rows_per_s``: batched ClickHouse inserts through
  ``insert_transactions_batch``, the exact function chain sync calls once per
  confirmed block (``ogmios_client._insert_block_with_retry``), so the
  benchmark reproduces the production batch shape: one call per block of
  ``txs_per_block`` transactions. The metric counts rows landed across the
  transactions + transaction_inputs + transaction_outputs tables; the
  utxo_features / tx_script_features side inserts run inside the timed region
  exactly as they do in production but are excluded from the count, keeping
  the reported throughput conservative.

The workload is deterministic: a fixed seed drives every drawn amount and
every identifier is derived from its (block, tx) coordinates, so a rerun
writes byte-identical rows under the same ReplacingMergeTree keys and merges
collapse them instead of growing the warehouse. Every row is namespaced under
PERF_NETWORK; all read paths are network-scoped, so operator dashboards never
surface the synthetic rows.
"""

import hashlib
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.ingestion.ogmios_parser import parse_ogmios_transaction
from perf import PERF_NETWORK, POLICY_ID_HEX_CHARS, WORKLOAD_SEED, results
from perf.config import load

# Pinned chain time for the synthetic history. Wall clock would stamp every
# rerun with fresh timestamps and fresh ReplacingMergeTree version columns,
# defeating dedup; a fixed base keeps replayed rows byte-identical.
_CHAIN_TIME_BASE = datetime(2026, 1, 1, tzinfo=UTC)

# Shelley-era slot length is one second (shelley genesis "slotLength": 1 on
# mainnet/preprod/preview). The synthetic chain forges one block per slot,
# denser than the real ~5% active-slot coefficient, which is harmless because
# nothing in the write path depends on inter-block spacing.
_SLOT_SECONDS = 1

# Typical mainnet fee band: the min-fee formula floors simple payments around
# 160k lovelace and script-heavy transactions stay under a million.
_FEE_MIN_LOVELACE = 160_000
_FEE_MAX_LOVELACE = 900_000

# Output values span min-UTxO-sized change (about 1 ada) up to a few thousand
# ada: the orders of magnitude ingestion actually sees between whale moves.
_OUTPUT_MIN_LOVELACE = 1_000_000
_OUTPUT_MAX_LOVELACE = 2_000_000_000

# Realistic structural spread: most on-chain transactions carry a handful of
# inputs and outputs; six keeps the mix inside the shapes the parser fixture
# tests pin while still varying row counts per tx.
_MAX_TX_INPUTS = 6
_MAX_TX_OUTPUTS = 6

# Collateral posted by script transactions: the ledger requires roughly 150%
# of the fee (collateralPercentage), so a few ada is the realistic magnitude.
_COLLATERAL_LOVELACE = 5_000_000

# Stake-key registration deposit: protocol parameter keyDeposit = 2 ada.
_KEY_DEPOSIT_LOVELACE = 2_000_000

# Hex of ASCII "TOKEN"; mirrors the parser test fixtures' asset name.
_ASSET_NAME_HEX = "544f4b454e"

# Token quantities from a single NFT up to fungible-token transfer sizes.
_ASSET_QTY_MAX = 1_000_000

# Inline datum size: mid-sized (well below the bloat-detection gates) so the
# feature extractor does representative per-output work without the datum
# dominating the payload's serialization cost.
_INLINE_DATUM_BYTES = 256

# Execution-unit draws stay below the mainnet per-transaction budget
# (maxTxExecutionUnits: memory 14e6, cpu steps 10e9).
_EXUNITS_MEM_MAX = 14_000_000
_EXUNITS_CPU_MAX = 10_000_000_000


def _hash_hex(*parts: object) -> str:
    """Deterministic 64-hex-char identifier (the width of a real blake2b-256
    tx hash) derived only from workload coordinates, so a rerun regenerates
    the exact same ReplacingMergeTree keys."""
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


def _address(*parts: object) -> str:
    """Deterministic synthetic address; the perf prefix makes any leak into a
    non-perf query trivially attributable."""
    return "addr_test1perf" + _hash_hex("addr", *parts)


def _datum_hex(tx_hash: str) -> str:
    """Deterministic inline-datum hex payload of _INLINE_DATUM_BYTES bytes
    (2 hex chars per byte)."""
    seed_hex = _hash_hex("datum", tx_hash)
    hex_chars = _INLINE_DATUM_BYTES * 2
    return (seed_hex * (hex_chars // len(seed_hex) + 1))[:hex_chars]


def _inputs(tx_hash: str, count: int) -> list[dict[str, Any]]:
    return [{"transaction": {"id": _hash_hex("src", tx_hash, i)}, "index": i} for i in range(count)]


def _simple_payment(rng: random.Random, tx_hash: str) -> dict[str, Any]:
    """The dominant on-chain shape: ada-only payment plus change."""
    return {
        "id": tx_hash,
        "spends": "inputs",
        "fee": {"ada": {"lovelace": rng.randint(_FEE_MIN_LOVELACE, _FEE_MAX_LOVELACE)}},
        "inputs": _inputs(tx_hash, rng.randint(1, _MAX_TX_INPUTS)),
        "outputs": [
            {
                "address": _address(tx_hash, i),
                "value": {
                    "ada": {"lovelace": rng.randint(_OUTPUT_MIN_LOVELACE, _OUTPUT_MAX_LOVELACE)}
                },
            }
            for i in range(rng.randint(1, _MAX_TX_OUTPUTS))
        ],
    }


def _native_asset_transfer(rng: random.Random, tx_hash: str) -> dict[str, Any]:
    """Native-asset payment carrying message metadata."""
    body = _simple_payment(rng, tx_hash)
    policy = _hash_hex("policy", tx_hash)[:POLICY_ID_HEX_CHARS]
    body["outputs"][0]["value"][policy] = {_ASSET_NAME_HEX: rng.randint(1, _ASSET_QTY_MAX)}
    # Label 674 is the CIP-20 transaction-message standard, the most common
    # metadata label ingestion sees in the wild.
    body["metadata"] = {"labels": {"674": {"json": {"msg": ["perf replay"]}}}}
    return body


def _script_spend(rng: random.Random, tx_hash: str) -> dict[str, Any]:
    """Plutus interaction: reference input, collateral, inline datum output,
    redeemers, and a mint, exercising every side table the write path fills."""
    body = _simple_payment(rng, tx_hash)
    policy = _hash_hex("mintpolicy", tx_hash)[:POLICY_ID_HEX_CHARS]
    body["references"] = [{"transaction": {"id": _hash_hex("ref", tx_hash)}, "index": 0}]
    body["collaterals"] = [{"transaction": {"id": _hash_hex("col", tx_hash)}, "index": 0}]
    body["collateralReturn"] = {
        "address": _address(tx_hash, "colret"),
        "value": {"ada": {"lovelace": _COLLATERAL_LOVELACE}},
    }
    body["outputs"][0]["datum"] = _datum_hex(tx_hash)
    body["mint"] = {policy: {_ASSET_NAME_HEX: rng.randint(1, _ASSET_QTY_MAX)}}
    body["redeemers"] = [
        {
            "validator": {"index": 0, "purpose": "spend"},
            "executionUnits": {
                "memory": rng.randint(1, _EXUNITS_MEM_MAX),
                "cpu": rng.randint(1, _EXUNITS_CPU_MAX),
            },
        }
    ]
    return body


def _failed_script(rng: random.Random, tx_hash: str) -> dict[str, Any]:
    """Phase-2 failure: the ledger consumed the collateral and created only
    the collateralReturn; regular inputs persist flagged is_unspent_attempt."""
    body = _script_spend(rng, tx_hash)
    body["spends"] = "collaterals"
    return body


def _delegation(rng: random.Random, tx_hash: str) -> dict[str, Any]:
    """Account operations: certificate, reward withdrawal, and key deposit."""
    body = _simple_payment(rng, tx_hash)
    body["certificates"] = [{"type": "stakeDelegation", "credential": _hash_hex("cred", tx_hash)}]
    body["withdrawals"] = {
        "stake_test1perf" + _hash_hex("stake", tx_hash): {
            "ada": {"lovelace": rng.randint(_OUTPUT_MIN_LOVELACE, _OUTPUT_MAX_LOVELACE)}
        }
    }
    body["deposit"] = {"ada": {"lovelace": _KEY_DEPOSIT_LOVELACE}}
    return body


# Cycled by global tx index, so the shape mix is deterministic and independent
# of how many random draws each builder consumes.
_SHAPE_BUILDERS = (
    _simple_payment,
    _native_asset_transfer,
    _script_spend,
    _failed_script,
    _delegation,
)


def _build_workload(blocks: int, txs_per_block: int) -> list[dict[str, Any]]:
    """Synthetic chain: ``blocks`` blocks of ``txs_per_block`` raw Ogmios v6
    payloads with per-block coordinates, mirroring what one nextBlock response
    hands the chain-sync client."""
    rng = random.Random(WORKLOAD_SEED)
    workload: list[dict[str, Any]] = []
    for block_i in range(blocks):
        txs = []
        for tx_i in range(txs_per_block):
            global_i = block_i * txs_per_block + tx_i
            builder = _SHAPE_BUILDERS[global_i % len(_SHAPE_BUILDERS)]
            txs.append(builder(rng, _hash_hex("tx", block_i, tx_i)))
        workload.append(
            {
                "hash": _hash_hex("block", block_i),
                "slot": block_i,
                "height": block_i,
                "timestamp": _CHAIN_TIME_BASE + timedelta(seconds=block_i * _SLOT_SECONDS),
                "txs": txs,
            }
        )
    return workload


def test_ingestion_replay(ch):
    budget = load().ingestion
    workload = _build_workload(budget.blocks, budget.txs_per_block)
    total_txs = budget.blocks * budget.txs_per_block

    # (a) Parse throughput: everything the timed loop touches is prebuilt;
    # the call shape mirrors ogmios_client._parse_block_txs.
    parsed_blocks: list[list] = []
    started = time.perf_counter()
    for block in workload:
        parsed_blocks.append(
            [
                parse_ogmios_transaction(
                    tx_data,
                    block_slot=block["slot"],
                    block_hash=block["hash"],
                    block_height=block["height"],
                    timestamp=block["timestamp"],
                    block_index=block_index,
                )
                for block_index, tx_data in enumerate(block["txs"])
            ]
        )
    parse_seconds = time.perf_counter() - started
    parse_tps = total_txs / parse_seconds

    # Post-parse stamping, outside both timed regions: chain sync assigns
    # tx.network the same way, and pinning ingestion_timestamp to chain time
    # (instead of the model's wall-clock default) fixes the ReplacingMergeTree
    # version column so a rerun's rows are byte-identical and merge away.
    for block, txs in zip(workload, parsed_blocks, strict=True):
        for tx in txs:
            tx.network = PERF_NETWORK
            tx.ingestion_timestamp = block["timestamp"]

    total_rows = sum(1 + len(tx.inputs) + len(tx.outputs) for txs in parsed_blocks for tx in txs)

    # (b) Insert throughput through the production write path: one
    # insert_transactions_batch call per block, the batch shape
    # _insert_block_with_retry hands to ClickHouse.
    started = time.perf_counter()
    for txs in parsed_blocks:
        ch.insert_transactions_batch(txs)
    insert_seconds = time.perf_counter() - started
    insert_rows_per_s = total_rows / insert_seconds

    # Recorded BEFORE the asserts: a failed run must still leave an artifact
    # for the performance report, judged on the same checks.
    checks = [
        results.check("parse_tps", parse_tps, ">=", budget.min_parse_tps),
        results.check("insert_rows_per_s", insert_rows_per_s, ">=", budget.min_insert_rows_per_s),
    ]
    results.record(
        "ingestion_replay",
        metrics={
            "parse_tps": parse_tps,
            "insert_rows_per_s": insert_rows_per_s,
            "parse_seconds": parse_seconds,
            "insert_seconds": insert_seconds,
            "blocks": budget.blocks,
            "txs_per_block": budget.txs_per_block,
            "total_txs": total_txs,
            "total_rows": total_rows,
            "workload_seed": WORKLOAD_SEED,
        },
        checks=checks,
    )
    assert parse_tps >= budget.min_parse_tps, (
        f"parse throughput {parse_tps:.0f} tx/s is below the "
        f"min_parse_tps budget of {budget.min_parse_tps}"
    )
    assert insert_rows_per_s >= budget.min_insert_rows_per_s, (
        f"insert throughput {insert_rows_per_s:.0f} rows/s is below the "
        f"min_insert_rows_per_s budget of {budget.min_insert_rows_per_s}"
    )
