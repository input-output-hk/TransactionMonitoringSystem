"""Tests for Blockfrost JSON -> ClickHouse row normalization."""

from __future__ import annotations

from app.blockfrost.normalize import build_records

TX_HASH = "a" * 64

TX_DETAIL = {
    "hash": TX_HASH,
    "block_height": 100,
    "block_time": 1_700_000_000,
    "slot": 5,
    "fees": "200000",
    "deposit": "0",
    "size": 500,
    "valid_contract": True,
    "redeemer_count": 1,
}

UTXOS = {
    "hash": TX_HASH,
    "inputs": [
        {
            "address": "addrA",
            "amount": [
                {"unit": "lovelace", "quantity": "5000000"},
                {"unit": "policy1tokenX", "quantity": "10"},
            ],
            "collateral": False,
            "reference": False,
        },
        # collateral input: NOT consumed on a successful tx (excluded here),
        # the ONLY consumed input on a failed tx (see the dedicated test).
        {
            "address": "addrCol",
            "amount": [{"unit": "lovelace", "quantity": "2000000"}],
            "collateral": True,
        },
    ],
    "outputs": [
        {
            "address": "addrB",
            "amount": [{"unit": "lovelace", "quantity": "3000000"}],
            "output_index": 0,
        },
        {
            "address": "addrA",
            "amount": [
                {"unit": "lovelace", "quantity": "1800000"},
                {"unit": "policy1tokenX", "quantity": "10"},
            ],
            "output_index": 1,
        },
    ],
}


def test_build_records_aggregates() -> None:
    tx, utxos, assets = build_records("addrA", "address", TX_DETAIL, UTXOS)

    assert tx.tx_hash == TX_HASH
    assert tx.input_count == 1  # collateral excluded
    assert tx.output_count == 2
    assert tx.total_input_lovelace == 5_000_000
    assert tx.total_output_lovelace == 4_800_000
    assert tx.distinct_input_addresses == 1
    assert tx.distinct_output_addresses == 2
    assert tx.distinct_assets == 1
    assert tx.redeemer_count == 1
    assert tx.valid_contract == 1
    assert tx.fees == 200_000

    # 1 real input UTXO + 2 outputs
    assert len(utxos) == 3
    assert {u.role for u in utxos} == {"input", "output"}
    # token appears in one input and one output
    assert len(assets) == 2
    assert all(a.unit == "policy1tokenX" for a in assets)


def test_invalid_contract_flag() -> None:
    detail = {**TX_DETAIL, "valid_contract": False}
    tx, _, _ = build_records("addrA", "address", detail, UTXOS)
    assert tx.valid_contract == 0


def test_failed_tx_consumes_collateral_inputs_not_regular() -> None:
    # On-chain, a script-failed tx consumes ONLY its collateral inputs; the
    # regular inputs remain unspent. Features and the co-spend graph must see the
    # collateral spender (who authorized and paid for the failed attempt), not
    # the phantom regular inputs.
    detail = {**TX_DETAIL, "valid_contract": False}
    tx, utxos, _ = build_records("addrA", "address", detail, UTXOS)
    inputs = [u for u in utxos if u.role == "input"]
    assert tx.valid_contract == 0
    assert tx.input_count == 1
    assert [u.address for u in inputs] == ["addrCol"]  # the collateral spender
    assert tx.total_input_lovelace == 2_000_000  # collateral value, not regular


def test_collateral_return_output_excluded() -> None:
    # On a script-failed tx the only "output" is the collateral return; it must
    # not be counted as a normal output (it would inflate output features).
    utxos = {
        "hash": TX_HASH,
        "inputs": [
            {"address": "addrA", "amount": [{"unit": "lovelace", "quantity": "5000000"}]}
        ],
        "outputs": [
            {
                "address": "addrB",
                "amount": [{"unit": "lovelace", "quantity": "3000000"}],
                "output_index": 0,
            },
            {
                "address": "addrCol",
                "amount": [{"unit": "lovelace", "quantity": "1000000"}],
                "output_index": 1,
                "collateral": True,
            },
        ],
    }
    tx, _, _ = build_records("addrA", "address", {**TX_DETAIL, "valid_contract": False}, utxos)
    assert tx.output_count == 1
    assert tx.total_output_lovelace == 3_000_000


def test_inputs_keyed_by_stable_onchain_index() -> None:
    # The same inputs fetched in different order must yield identical idx
    # assignments (sorted by consumed-UTXO identity), so re-ingest can't create
    # un-dedupable duplicate rows.
    a = {
        "address": "addrA", "amount": [{"unit": "lovelace", "quantity": "1"}],
        "tx_hash": "bb" * 32, "output_index": 1,
    }
    b = {
        "address": "addrB", "amount": [{"unit": "lovelace", "quantity": "1"}],
        "tx_hash": "aa" * 32, "output_index": 0,
    }
    _, u1, _ = build_records("t", "address", TX_DETAIL, {"hash": TX_HASH, "inputs": [a, b], "outputs": []})
    _, u2, _ = build_records("t", "address", TX_DETAIL, {"hash": TX_HASH, "inputs": [b, a], "outputs": []})
    idx1 = {u.address: u.idx for u in u1 if u.role == "input"}
    idx2 = {u.address: u.idx for u in u2 if u.role == "input"}
    assert idx1 == idx2  # order-independent
    assert idx1["addrB"] == 0 and idx1["addrA"] == 1  # aa.. sorts before bb..


def test_malformed_amount_entry_does_not_raise() -> None:
    # A missing quantity/unit must not abort the whole ingest.
    utxos = {
        "hash": TX_HASH,
        "inputs": [
            {"address": "addrA", "amount": [{"unit": "lovelace"}, {"quantity": "5"}]}
        ],
        "outputs": [],
    }
    tx, _, assets = build_records("addrA", "address", TX_DETAIL, utxos)
    assert tx.total_input_lovelace == 0  # missing quantity -> 0
    assert assets == []  # entry with no unit is skipped
