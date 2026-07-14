"""Unit tests for shared feature helpers: script-address detection and
Ogmios v5/v6 field extraction."""

from app.analysis.features import (
    SCRIPT_ADDRESS_PREFIXES,
    extract_fee,
    extract_ttl,
    extract_tx_script_features,
    flatten_assets,
    is_script_address,
    iter_assets,
)


class TestScriptFeaturesHostileRedeemers:
    """One malformed redeemer entry must cost only its own units, never
    abort extraction: this runs inside batch ingestion, where a raise
    used to drop the whole block's feature rows (review finding)."""

    def test_non_dict_list_entries_degrade_to_zero_units(self):
        raw = {
            "inputs": [],
            "redeemers": [
                "junk",
                {"executionUnits": {"memory": 2, "cpu": 3}},
                {"executionUnits": {"cpu": "abc"}},
            ],
        }
        row = extract_tx_script_features("ab" * 32, "preprod", raw)
        # (tx_hash, network, redeemers_count, spending_inputs,
        #  exunits_mem, exunits_cpu, mint_policy_count, mint_entries_json)
        assert row is not None
        assert row[2] == 3  # count still reflects every entry
        assert row[4] == 2 and row[5] == 3  # units only from the valid one

    def test_non_dict_dict_values_degrade_to_zero_units(self):
        raw = {
            "inputs": [],
            "redeemers": {
                "spend:0": {"executionUnits": {"memory": 4, "cpu": 6}},
                "spend:1": "garbage",
            },
        }
        row = extract_tx_script_features("ab" * 32, "preprod", raw)
        assert row is not None
        assert row[4] == 4 and row[5] == 6


class TestIsScriptAddress:
    """CIP-19: payment credential is a script for header types 1/3/5/7."""

    def test_enterprise_script_type7(self):
        assert is_script_address("addr1w9qzpelu9hn45pefc0xr4ac4kd") is True
        assert is_script_address("addr_test1wq0zpelu9hn45pefc0xr4") is True

    def test_script_with_stake_key_type1(self):
        assert is_script_address("addr1z8snz7c4974vzdpxu65ruph") is True
        assert is_script_address("addr_test1z8snz7c4974vzdpxu65") is True

    def test_script_with_script_stake_type3(self):
        assert is_script_address("addr1x8snz7c4974vzdpxu65ruph") is True
        assert is_script_address("addr_test1x8snz7c4974vzdpxu65") is True

    def test_script_with_pointer_stake_type5(self):
        assert is_script_address("addr128snz7c4974vzdpxu65ruph") is True
        assert is_script_address("addr_test128snz7c4974vzdpxu6") is True

    def test_payment_key_with_script_stake_type2_excluded(self):
        # 'y' is payment-KEY + script-stake: the spending credential is a
        # key, so script-targeted attacks do not apply.
        assert is_script_address("addr1y8snz7c4974vzdpxu65ruph") is False
        assert is_script_address("addr_test1y8snz7c4974vzdpxu65") is False

    def test_wallet_addresses_excluded(self):
        assert is_script_address("addr1qx2fxv2umyhttkxyxp8x0dlpdt3") is False
        assert is_script_address("addr_test1qq2fxv2umyhttkxyxp8x0") is False
        assert is_script_address("addr1v8snz7c4974vzdpxu65ruph") is False

    def test_byron_and_empty_excluded(self):
        assert is_script_address("Ae2tdPwUPEZ18ZjTLnLVr9CEvUEUX4eW1") is False
        assert is_script_address("DdzFFzCqrhsw3prhfMFDNFowbzUku3QmrM") is False
        assert is_script_address("") is False

    def test_prefix_tuple_covers_both_networks(self):
        mainnet = [p for p in SCRIPT_ADDRESS_PREFIXES if "_test" not in p]
        testnet = [p for p in SCRIPT_ADDRESS_PREFIXES if "_test" in p]
        assert len(mainnet) == len(testnet) == 4


class TestExtractTtl:
    def test_v6_validity_interval(self):
        assert extract_ttl({"validityInterval": {"invalidAfter": 1000}}) == 1000

    def test_v6_invalid_before_only(self):
        # invalidBefore alone carries no TTL; fall through to 0.
        assert extract_ttl({"validityInterval": {"invalidBefore": 5}}) == 0

    def test_v5_time_to_live(self):
        assert extract_ttl({"timeToLive": 1234}) == 1234

    def test_v6_preferred_over_v5(self):
        tx = {"validityInterval": {"invalidAfter": 77}, "timeToLive": 1234}
        assert extract_ttl(tx) == 77

    def test_absent_returns_zero(self):
        assert extract_ttl({}) == 0
        assert extract_ttl(None) == 0
        assert extract_ttl({"validityInterval": None}) == 0


class TestExtractFee:
    def test_v6_nested(self):
        assert extract_fee({"fee": {"ada": {"lovelace": 200_000}}}) == 200_000

    def test_v5_flat(self):
        assert extract_fee({"fee": {"lovelace": 170_000}}) == 170_000

    def test_bare_number(self):
        assert extract_fee({"fee": 150_000}) == 150_000

    def test_absent(self):
        assert extract_fee({}) == 0
        assert extract_fee(None) == 0


class TestFlattenAssets:
    def test_nested_bundles_flatten_to_dotted_keys(self):
        value = {
            "ada": {"lovelace": 2_000_000},
            "p1": {"aa": 5, "bb": 1},
            "p2": {"cc": 7},
        }
        assert flatten_assets(value) == {"p1.aa": 5, "p1.bb": 1, "p2.cc": 7}

    def test_flat_legacy_entries_pass_through(self):
        assert flatten_assets({"lovelace": 1, "p1.aa": 3}) == {"p1.aa": 3}

    def test_ada_only(self):
        assert flatten_assets({"ada": {"lovelace": 9}}) == {}
        assert flatten_assets("not a dict") == {}

    def test_malformed_quantity_degrades_to_zero_keeps_tx(self):
        # Recall-first: a malformed asset quantity in untrusted chain data must
        # NOT raise (raising would drop the whole tx from the warehouse). It
        # degrades to 0 with the asset key preserved, mirroring extract_lovelace.
        nested = {"p1": {"aa": "not-an-int"}, "p2": {"cc": 7}}
        assert flatten_assets(nested) == {"p1.aa": 0, "p2.cc": 7}
        flat = {"p1.aa": None, "p2.bb": 4}
        assert flatten_assets(flat) == {"p1.aa": 0, "p2.bb": 4}


class TestIterAssets:
    def test_yields_policy_name_qty(self):
        value = {"ada": {"lovelace": 1}, "p1": {"aa": 2, "bb": 3}}
        assert dict(iter_assets(value)) == {("p1", "aa"): 2, ("p1", "bb"): 3}

    def test_skips_unparseable_and_flat(self):
        value = {"lovelace": 1, "p1": {"aa": "garbage"}, "p2.flat": 5}
        assert list(iter_assets(value)) == []
        assert list(iter_assets(None)) == []

    def test_garbage_quantities_degrade_to_zero(self):
        # Untrusted chain data must never abort a parse (recall-first):
        # the dict path previously raised on non-numeric quantities while
        # the docstring promised 0.
        from app.analysis.features import extract_lovelace

        assert extract_lovelace({"ada": {"lovelace": "garbage"}}) == 0
        assert extract_lovelace({"lovelace": {"x": 1}}) == 0
        assert extract_lovelace({"ada": {"lovelace": None}}) == 0


class TestDatumWitnessAndObjectDatum:
    """Datum sizing / bloat discriminators cover the witness-preimage and
    object-shaped-datum cases (previously reported 0 bytes / not-assessable)."""

    def test_datum_hash_sized_from_witness_preimage_hex(self):
        from app.analysis.features import _extract_datum_info

        out = {"address": "addr1w...", "datumHash": "abc123"}
        # No witness map -> unsizable (unchanged behaviour).
        assert _extract_datum_info(out) == (1, 0)
        # Preimage carried in the tx witness datums (hex) -> sized.
        datums = {"abc123": "de" * 4000}  # 8000 hex chars -> 4000 bytes
        assert _extract_datum_info(out, datums) == (1, 4000)

    def test_datum_hash_sized_from_witness_preimage_object(self):
        from app.analysis.features import _extract_datum_info

        out = {"address": "addr1w...", "datumHash": "h1"}
        preimage = {"bytes": "00" * 5000}
        flag, size = _extract_datum_info(out, {"h1": preimage})
        assert flag == 1
        assert size > 5000  # json.dumps of the object

    def test_object_datum_entropy_is_assessable(self):
        from app.analysis.features import datum_shannon_entropy_bits

        # Object datum with a large all-zero ByteArray leaf: low entropy.
        low = {"constructor": 0, "fields": [{"bytes": "00" * 4000}]}
        assert datum_shannon_entropy_bits({"datum": low}) < 1.0
        # Object datum with a high-entropy random-ish leaf: near the ceiling.
        import os

        rand_hex = os.urandom(4000).hex()
        high = {"constructor": 0, "fields": [{"bytes": rand_hex}]}
        assert datum_shannon_entropy_bits({"datum": high}) > 7.0

    def test_object_datum_leaf_concentration(self):
        from app.analysis.features import datum_leaf_concentration

        # One giant leaf + a few tiny ones -> concentration near 1.0.
        datum = {
            "list": [
                {"bytes": "ab" * 5000},
                {"bytes": "cd"},
                {"bytes": "ef"},
            ]
        }
        assert datum_leaf_concentration({"datum": datum}) > 0.99
