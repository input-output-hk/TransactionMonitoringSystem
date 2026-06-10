"""Tests for mempool UTxO resolution parsing (queryLedgerState/utxo)."""

from app.ingestion.ogmios_client import _parse_resolved_utxo

POLICY = "a" * 56


class TestParseResolvedUtxo:
    def test_v6_ada_nested_value(self):
        # Regression: the v5-only read returned lovelace=0 for every v6 UTxO
        # and mis-filed the "ada" sub-dict as a native asset, so every
        # mempool-resolved input carried amount=0 and total_input_value
        # stayed NULL on v6 nodes.
        utxo = {
            "transaction": {"id": "11" * 32},
            "index": 3,
            "address": "addr_test1qqowner",
            "value": {"ada": {"lovelace": 5_000_000}, POLICY: {"deadbeef": 7}},
        }
        ref, resolved = _parse_resolved_utxo(utxo)
        assert ref == ("11" * 32, 3)
        assert resolved["address"] == "addr_test1qqowner"
        assert resolved["amount"] == 5_000_000
        assert resolved["assets"] == {f"{POLICY}.deadbeef": 7}
        assert "ada.lovelace" not in (resolved["assets"] or {})

    def test_v5_flat_value(self):
        utxo = {
            "transaction": {"id": "22" * 32},
            "index": 0,
            "address": "addr_test1qqowner",
            "value": {"lovelace": 1_200_000},
        }
        _, resolved = _parse_resolved_utxo(utxo)
        assert resolved["amount"] == 1_200_000
        assert resolved["assets"] is None

    def test_ada_only_v6_has_no_assets(self):
        utxo = {
            "transaction": {"id": "33" * 32},
            "index": 1,
            "address": "addr_test1qqowner",
            "value": {"ada": {"lovelace": 9_000_000}},
        }
        _, resolved = _parse_resolved_utxo(utxo)
        assert resolved["amount"] == 9_000_000
        assert resolved["assets"] is None
