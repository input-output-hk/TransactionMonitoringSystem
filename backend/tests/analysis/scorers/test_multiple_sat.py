"""Unit tests for the Multiple Satisfaction scorer (Class 4)."""

import pytest
from app.analysis.normalise import (
    BAND_HIGH_THRESHOLD,
    BAND_MODERATE_MAX,
    BAND_MODERATE_THRESHOLD,
)
from app.analysis.scorers.multiple_sat import (
    MultipleSatScorer,
    _W as _WEIGHTS,
    _compute_n_assets_out,
    _iter_assets,
    _reweight_without_extraction,
    _spend_redeemer_payloads,
)

_W_EXTRACTION = float(_WEIGHTS["extraction"])
_W_EXUNITS = float(_WEIGHTS["exunits_inv"])
_W_INPUTS = float(_WEIGHTS["inputs"])
_W_RECURRENCE = float(_WEIGHTS["recurrence"])


@pytest.fixture
def scorer():
    return MultipleSatScorer()


SCRIPT = "addr_test1wz5fxvalex"
WALLET = "addr_test1qz5fxvalex"


def _features(inputs, outputs=None, redeemers=None, sender_recurrence=0.0, network="preprod"):
    return {
        "tx_hash": "ms01",
        "network": network,
        "sender_recurrence": sender_recurrence,
        "raw_data": {
            "inputs": inputs,
            "outputs": outputs or [],
            "redeemers": redeemers,
        },
    }


class TestSpendRedeemerPayloads:
    """Ogmios v5 carries the redeemer purpose in the key ("spend:N"), not on
    the value; live v6 (v6.14) emits a list with validator.purpose on each
    entry. A prior bug looked for purpose on the value only, so the keyed
    shape returned [], disabling the uniform-sweep guard and zeroing
    redeemer evidence. These pin v5/v6 parity."""

    def test_v5_dict_extracts_spend_payloads_by_key(self):
        redeemers = {
            "spend:0": {"redeemer": "d8799f00ff", "executionUnits": {"memory": 1, "cpu": 1}},
            "spend:1": {"redeemer": "d8799f01ff", "executionUnits": {"memory": 1, "cpu": 1}},
            "mint:0": {"redeemer": "d8799fffff", "executionUnits": {"memory": 1, "cpu": 1}},
        }
        assert _spend_redeemer_payloads({"redeemers": redeemers}) == [
            "d8799f00ff", "d8799f01ff",
        ]

    def test_v5_uniform_payloads_collapse_to_one(self):
        redeemers = {
            "spend:0": {"redeemer": "aa"},
            "spend:1": {"redeemer": "aa"},
        }
        payloads = _spend_redeemer_payloads({"redeemers": redeemers})
        assert len(payloads) == 2 and len(set(payloads)) == 1

    def test_v6_list_extracts_spend(self):
        redeemers = [
            {"validator": {"purpose": "spend"}, "redeemer": "aa"},
            {"validator": {"purpose": "mint"}, "redeemer": "bb"},
            {"purpose": "spend", "redeemer": "cc"},
        ]
        assert _spend_redeemer_payloads({"redeemers": redeemers}) == ["aa", "cc"]

    def test_empty_and_missing(self):
        assert _spend_redeemer_payloads({}) == []
        assert _spend_redeemer_payloads({"redeemers": None}) == []


class TestGate:
    def test_no_raw_data(self, scorer):
        assert scorer.gate({"raw_data": None}) is False

    def test_single_script_input_rejected(self, scorer):
        inputs = [{"address": SCRIPT, "value": {"lovelace": 5_000_000}}]
        assert scorer.gate(_features(inputs)) is False

    def test_two_wallet_inputs_rejected(self, scorer):
        inputs = [
            {"address": WALLET, "value": {"lovelace": 5_000_000}},
            {"address": WALLET, "value": {"lovelace": 5_000_000}},
        ]
        assert scorer.gate(_features(inputs)) is False

    def test_two_script_inputs_with_redeemers_passes(self, scorer):
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
        ]
        redeemers = {
            "spend:0": {"executionUnits": {"memory": 1, "cpu": 1}},
            "spend:1": {"executionUnits": {"memory": 1, "cpu": 1}},
        }
        assert scorer.gate(_features(inputs, redeemers=redeemers)) is True

    def test_native_script_inputs_without_redeemers_rejected(self, scorer):
        # Multisig / timelock native scripts evaluate per-input with no
        # validator code, so multiple-satisfaction is structurally impossible.
        # The gate must skip them; otherwise normal multisig consolidation
        # txs (12 inputs into 1 output) trigger Critical false positives.
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
        ]
        assert scorer.gate(_features(inputs, redeemers=None)) is False
        assert scorer.gate(_features(inputs, redeemers={})) is False

    def test_mint_only_redeemer_does_not_satisfy_gate(self, scorer):
        # A tx with only a mint redeemer (and native-script inputs) is still
        # a native-script spend; the spend redeemer is what matters.
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
        ]
        redeemers = {"mint:0": {"executionUnits": {"memory": 1, "cpu": 1}}}
        assert scorer.gate(_features(inputs, redeemers=redeemers)) is False

    def test_same_payment_cred_different_stake_cred_groups_together(self, scorer):
        # Regression: the canonical purchase-offer double-satisfaction shape
        # spends two UTxOs at the same validator deployed under different
        # stake credentials, putting them at distinct ``address`` strings
        # but the same script. Grouping by raw address misses this; we now
        # group by payment credential. Uses real preprod addresses captured
        # from a representative exploit run (same payment cred, different
        # stake parts).
        addr_a = (
            "addr_test1zpsqdy4efletcs8d6pgzjrxmjq6gg82dr5fyvepn9yv09l"
            "d285x8fy9ezxxyczxq0rfc3m5rfl6yj6ex3ecxx70xngnsf52z3z"
        )
        addr_b = (
            "addr_test1zpsqdy4efletcs8d6pgzjrxmjq6gg82dr5fyvepn9yv09l"
            "vysjzwzgewp6evhc7rl83l3z5ftvhfeuhmt29sxgxh3yzqkesp9d"
        )
        inputs = [
            {"address": addr_a, "value": {"lovelace": 10_000_000}},
            {"address": addr_b, "value": {"lovelace": 10_000_000}},
        ]
        redeemers = {
            "spend:0": {"executionUnits": {"memory": 1, "cpu": 1}},
            "spend:1": {"executionUnits": {"memory": 1, "cpu": 1}},
        }
        assert scorer.gate(_features(inputs, redeemers=redeemers)) is True


class TestScore:
    def test_sub_score_keys(self, scorer):
        """sub_scores should be s_extraction / s_exunits_inv / s_inputs / s_recurrence."""
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
        ]
        redeemers = {
            "spend:0": {"executionUnits": {"memory": 50_000, "cpu": 100_000}},
            "spend:1": {"executionUnits": {"memory": 50_000, "cpu": 100_000}},
        }
        result = scorer.score(_features(inputs, redeemers=redeemers))
        expected = {"s_extraction", "s_exunits_inv", "s_inputs", "s_recurrence"}
        assert expected.issubset(result.sub_scores.keys())
        assert "redeemer_input_ratio_inv" not in result.sub_scores
        assert "full_drain" not in result.sub_scores

    def test_value_extraction_boosts_score(self, scorer):
        """Large net value leaving script should boost s_extraction."""
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 100_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 100_000_000}},
        ]
        outputs = [
            {"address": WALLET, "value": {"lovelace": 195_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 2_000_000}},
        ]
        redeemers = {"spend:0": {"executionUnits": {"memory": 50000, "cpu": 100000}}}
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.sub_scores["s_extraction"] > 0.3
        assert result.score > 0

    def test_native_asset_extraction_scores_high(self, scorer):
        """NFT-marketplace double-sat shape: assets leave the script, lovelace
        position is flat. The asset axis must carry the signal where the
        lovelace axis bottoms out. Mirrors the canonical NFT-marketplace case.
        """
        policy_a = "33776c029a27667146c43531a69e2e0bd4affa384dc96e2fb8508c17"
        policy_b = "07c2650ee55434e578fdd328a1f794504359af3730a278842f5a4865"
        nft_a = "62386465383230393638202d2d204e465432"
        nft_b = "62386465383230393638202d2d204e465431"
        inputs = [
            {"address": SCRIPT, "value": {"ada": {"lovelace": 2_000_000}, policy_a: {nft_a: 1}}},
            {"address": SCRIPT, "value": {"ada": {"lovelace": 2_000_000}, policy_b: {nft_b: 1}}},
        ]
        # Buyer gets both NFTs; seller gets one underpayment. No NFT returns to script.
        outputs = [
            {"address": "addr_test1seller", "value": {"ada": {"lovelace": 50_000_000}}},
            {"address": WALLET, "value": {"ada": {"lovelace": 9_949_536_255},
                                          policy_a: {nft_a: 1}, policy_b: {nft_b: 1}}},
        ]
        redeemers = [
            {"validator": {"index": 0, "purpose": "spend"},
             "executionUnits": {"memory": 76_719, "cpu": 23_209_173}},
            {"validator": {"index": 1, "purpose": "spend"},
             "executionUnits": {"memory": 76_719, "cpu": 23_209_173}},
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.sub_scores["n_assets_out_of_script"] == 2.0
        assert result.sub_scores["s_extraction_assets"] == 1.0
        assert result.sub_scores["s_extraction_lov"] == 0.0
        assert result.sub_scores["s_extraction"] == 1.0
        assert "native_asset_extraction" in result.reasons
        # extraction weight is 0.42 → final score ≈ 42 (Moderate band)
        assert 35.0 <= result.score <= 50.0

    def test_native_asset_extraction_same_policy(self, scorer):
        """Same-policy NFT collection: two NFTs from one collection sold by
        one marketplace. Exercises the flow-accumulation path differently
        than the cross-policy case (one policy key with two asset_names).
        """
        policy = "33776c029a27667146c43531a69e2e0bd4affa384dc96e2fb8508c17"
        nft_a = "6e66743031"  # "nft01"
        nft_b = "6e66743032"  # "nft02"
        inputs = [
            {"address": SCRIPT, "value": {"ada": {"lovelace": 2_000_000}, policy: {nft_a: 1}}},
            {"address": SCRIPT, "value": {"ada": {"lovelace": 2_000_000}, policy: {nft_b: 1}}},
        ]
        outputs = [
            {"address": "addr_test1seller", "value": {"ada": {"lovelace": 50_000_000}}},
            {"address": WALLET, "value": {"ada": {"lovelace": 9_000_000_000},
                                          policy: {nft_a: 1, nft_b: 1}}},
        ]
        result = scorer.score(_features(inputs, outputs))
        # Two distinct (policy, name) pairs leaving the script.
        assert result.sub_scores["n_assets_out_of_script"] == 2.0
        assert result.sub_scores["s_extraction_assets"] == 1.0
        assert "native_asset_extraction" in result.reasons

    def test_high_n_inputs_same_script_scores_high(self, scorer):
        """Many inputs from the same script should push s_inputs toward 1.0."""
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}}
            for _ in range(10)
        ]
        redeemers = {
            f"spend:{i}": {"executionUnits": {"memory": 50_000, "cpu": 100_000}}
            for i in range(10)
        }
        result = scorer.score(_features(inputs, redeemers=redeemers))
        # n_inputs=10 with bootstrap anchors (2, 10) → s_inputs normalised to 1.0
        assert result.sub_scores["s_inputs"] >= 0.9
        assert result.sub_scores["n_inputs_same_script"] == 10

    def test_low_exunits_per_input_scores_high(self, scorer):
        """Many script inputs with very low total CPU → s_exunits_inv near 1.0."""
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}}
            for _ in range(5)
        ]
        # Total CPU 1000 across 5 inputs = 200 CPU/input, well below bootstrap p50=100_000
        redeemers = {
            "spend:0": {"executionUnits": {"memory": 100, "cpu": 1000}},
        }
        result = scorer.score(_features(inputs, redeemers=redeemers))
        assert result.sub_scores["s_exunits_inv"] >= 0.9

    def test_sender_recurrence_feeds_into_score(self, scorer):
        """sender_recurrence from features should feed s_recurrence."""
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
        ]
        result_zero = scorer.score(_features(inputs, sender_recurrence=0.0))
        result_high = scorer.score(_features(inputs, sender_recurrence=1.0))
        assert result_high.sub_scores["s_recurrence"] > result_zero.sub_scores["s_recurrence"]

    def test_allowlisted_script_reduces_extraction_weight(self, scorer):
        """Allowlisted scripts neutralise s_extraction; weight redistributes."""
        batch_addr = "addr1w9zsmyfc5tg49ng9gqaetm8qheyheemxakq47x7qfwnq5wq_full"
        inputs = [
            {"address": batch_addr, "value": {"lovelace": 100_000_000}}
            for _ in range(3)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 290_000_000}}]
        redeemers = {"spend:0": {"executionUnits": {"memory": 50000, "cpu": 100000}}}
        result = scorer.score(_features(inputs, outputs, redeemers, network="mainnet"))
        # s_extraction forced to 0 by allowlist reweight
        assert result.sub_scores["s_extraction"] == 0.0
        assert "allowlisted_batch_script" in result.reasons

    def test_allowlist_is_network_scoped(self, scorer):
        """A mainnet allowlist entry must not suppress an identical tx on preprod."""
        batch_addr = "addr1w9zsmyfc5tg49ng9gqaetm8qheyheemxakq47x7qfwnq5wq_full"
        inputs = [
            {"address": batch_addr, "value": {"lovelace": 100_000_000}}
            for _ in range(3)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 290_000_000}}]
        redeemers = {"spend:0": {"executionUnits": {"memory": 50000, "cpu": 100000}}}
        result = scorer.score(_features(inputs, outputs, redeemers, network="preprod"))
        assert "allowlisted_batch_script" not in result.reasons

    def test_allowlisted_score_lower_than_equivalent_non_allowlisted(self, scorer):
        """Compared to a non-allowlisted tx with identical extraction, allowlist lowers score."""
        allow_addr = "addr1w9zsmyfc5tg49ng9gqaetm8qheyheemxakq47x7qfwnq5wq_full"
        non_allow = "addr_test1wSOME_OTHER_SCRIPT_addr_12345"
        inputs_allow = [
            {"address": allow_addr, "value": {"lovelace": 500_000_000}}
            for _ in range(3)
        ]
        inputs_non = [
            {"address": non_allow, "value": {"lovelace": 500_000_000}}
            for _ in range(3)
        ]
        outputs_allow = [{"address": WALLET, "value": {"lovelace": 1_490_000_000}}]
        outputs_non = [{"address": WALLET, "value": {"lovelace": 1_490_000_000}}]
        redeemers = {"spend:0": {"executionUnits": {"memory": 50000, "cpu": 100000}}}
        r_allow = scorer.score(_features(inputs_allow, outputs_allow, redeemers, network="mainnet"))
        r_non = scorer.score(_features(inputs_non, outputs_non, redeemers, network="mainnet"))
        assert r_allow.score < r_non.score


class TestLazyValidatorBandFloor:
    """When the gate fires and s_exunits_inv saturates (lazy-validator
    fingerprint), the final score is floored to at least the High band.
    Mirrors the calibration applied to TMS-Forge synthetic exploits with
    minimal redeemer CPU.
    """

    def test_lazy_validator_floors_to_high_band(self, scorer):
        # 4 same-script inputs, minimal CPU per redeemer (< p50=100k).
        # Without the floor this would score ~32 (Moderate); with the floor
        # it must reach at least the High band threshold (60).
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 2_700_000}}
            for _ in range(4)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 10_000_000}}]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "executionUnits": {"memory": 600, "cpu": 100}}
            for i in range(4)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.sub_scores["s_exunits_inv"] > 0.8
        assert result.score >= BAND_HIGH_THRESHOLD
        assert "lazy_validator_band_floor" in result.reasons

    def test_floor_does_not_apply_when_validator_did_real_work(self, scorer):
        # Real validator CPU (well above p99=10M) → s_exunits_inv = 0 →
        # floor must NOT trigger. Mirrors the canonical NFT-marketplace case
        # where the score should stay at its weighted-average value.
        policy = "33776c029a27667146c43531a69e2e0bd4affa384dc96e2fb8508c17"
        nft_a = "6e66743031"
        nft_b = "6e66743032"
        inputs = [
            {"address": SCRIPT, "value": {"ada": {"lovelace": 2_000_000}, policy: {nft_a: 1}}},
            {"address": SCRIPT, "value": {"ada": {"lovelace": 2_000_000}, policy: {nft_b: 1}}},
        ]
        outputs = [
            {"address": "addr_test1seller", "value": {"ada": {"lovelace": 50_000_000}}},
            {"address": WALLET, "value": {"ada": {"lovelace": 9_000_000_000},
                                          policy: {nft_a: 1, nft_b: 1}}},
        ]
        redeemers = [
            {"validator": {"index": 0, "purpose": "spend"},
             "executionUnits": {"memory": 76_719, "cpu": 23_209_173}},
            {"validator": {"index": 1, "purpose": "spend"},
             "executionUnits": {"memory": 76_719, "cpu": 23_209_173}},
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.sub_scores["s_exunits_inv"] == 0.0
        assert result.score < BAND_HIGH_THRESHOLD
        assert "lazy_validator_band_floor" not in result.reasons

    def test_floor_does_not_apply_to_allowlisted_scripts(self, scorer):
        # Legitimate batchers run minimal per-input CPU by design (e.g. a
        # DEX settlement script that aggregates orders); the floor must not
        # punish them just because s_exunits_inv saturates.
        from app.analysis.scorers.multiple_sat import _ALLOWLIST
        mainnet_prefixes = _ALLOWLIST.get("mainnet", ())
        assert mainnet_prefixes, "test requires at least one mainnet allowlist entry"
        allowlisted_addr = mainnet_prefixes[0]
        inputs = [
            {"address": allowlisted_addr, "value": {"lovelace": 5_000_000}}
            for _ in range(4)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 20_000_000}}]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "executionUnits": {"memory": 600, "cpu": 100}}
            for i in range(4)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers, network="mainnet"))
        assert "allowlisted_batch_script" in result.reasons
        assert "lazy_validator_band_floor" not in result.reasons
        assert result.score < BAND_HIGH_THRESHOLD


class TestAssetHelpers:
    """Direct unit tests for the asset-extraction helpers, isolated from the
    scorer pipeline so shape-handling regressions surface immediately.
    """

    def test_iter_assets_v6_shape(self):
        val = {"ada": {"lovelace": 2_000_000}, "policy_x": {"asset_y": 3}}
        assert list(_iter_assets(val)) == [(("policy_x", "asset_y"), 3)]

    def test_iter_assets_v5_shape(self):
        val = {"lovelace": 2_000_000, "policy_x": {"asset_y": 3}}
        assert list(_iter_assets(val)) == [(("policy_x", "asset_y"), 3)]

    def test_iter_assets_skips_non_dict_policy_entry(self):
        val = {"ada": {"lovelace": 1}, "policy_x": "not a dict"}
        assert list(_iter_assets(val)) == []

    def test_iter_assets_skips_unparseable_qty(self):
        val = {"policy_x": {"asset_y": None, "asset_z": 4}}
        assert list(_iter_assets(val)) == [(("policy_x", "asset_z"), 4)]

    def test_iter_assets_empty_or_invalid(self):
        assert list(_iter_assets({})) == []
        assert list(_iter_assets(None)) == []
        assert list(_iter_assets("not a dict")) == []

    def test_n_assets_out_counts_pairs_not_units(self):
        """50 fungible-token units leaving = 1 pair, same as a single NFT."""
        inputs = [{"address": SCRIPT, "value": {"policy_x": {"asset_y": 50}}}]
        outputs = []
        assert _compute_n_assets_out(inputs, outputs, SCRIPT) == 1

    def test_n_assets_out_zero_when_continuation(self):
        """Asset enters the script and an equal qty leaves to script: net 0."""
        inputs = [{"address": SCRIPT, "value": {"policy_x": {"asset_y": 1}}}]
        outputs = [{"address": SCRIPT, "value": {"policy_x": {"asset_y": 1}}}]
        assert _compute_n_assets_out(inputs, outputs, SCRIPT) == 0

    def test_n_assets_out_ignores_non_script_addresses(self):
        inputs = [{"address": WALLET, "value": {"policy_x": {"asset_y": 1}}}]
        outputs = [{"address": WALLET, "value": {"policy_x": {"asset_y": 1}}}]
        assert _compute_n_assets_out(inputs, outputs, SCRIPT) == 0

    def test_n_assets_out_negative_net_does_not_count(self):
        """Asset only entering the script (not leaving) means net < 0; not extraction."""
        inputs = []
        outputs = [{"address": SCRIPT, "value": {"policy_x": {"asset_y": 1}}}]
        assert _compute_n_assets_out(inputs, outputs, SCRIPT) == 0


class TestWeights:
    def test_weights_sum_to_one(self):
        total = _W_EXTRACTION + _W_EXUNITS + _W_INPUTS + _W_RECURRENCE
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_weight_values_match_tracked_yaml(self):
        """Spec-weight regression guard: the tracked detection.yaml must
        always carry the documented default weights for Multiple Satisfaction."""
        import pathlib
        import yaml

        here = pathlib.Path(__file__).resolve()
        cfg_path = next(
            p for p in here.parents
            if (p / "config" / "detection.yaml").exists()
        ) / "config" / "detection.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        w = data["scorers"]["multiple_sat"]["weights"]
        assert w["extraction"] == 0.42
        assert w["exunits_inv"] == 0.28
        assert w["inputs"] == 0.16
        assert w["recurrence"] == 0.14

    def test_reweight_without_extraction_sums_to_one(self):
        w_ex, w_eu, w_ni, w_rc = _reweight_without_extraction()
        assert w_ex == 0.0
        assert (w_eu + w_ni + w_rc) == pytest.approx(1.0, abs=1e-9)

    def test_reweight_preserves_exunits_weight(self):
        _, w_eu, _, _ = _reweight_without_extraction()
        assert w_eu == _W_EXUNITS

    def test_reweight_distributes_extraction_by_ratio(self):
        """Bonus mass should split by the s_inputs:s_recurrence ratio (0.16:0.14)."""
        _, _, w_ni, w_rc = _reweight_without_extraction()
        bonus_inputs = w_ni - _W_INPUTS
        bonus_recurrence = w_rc - _W_RECURRENCE
        # The two bonuses together must equal the redistributed extraction mass.
        assert (bonus_inputs + bonus_recurrence) == pytest.approx(_W_EXTRACTION, abs=1e-9)
        # And their ratio must match the original 0.16 / 0.14.
        assert (bonus_inputs / bonus_recurrence) == pytest.approx(
            _W_INPUTS / _W_RECURRENCE, abs=1e-9,
        )


# A bech32-decodable script address whose payment credential resolves
# to the same 28-byte hash for every input. Using a real-looking preprod
# address keeps `_payment_credential` on the decode path so the
# uniform-sweep guard's `_is_decoded_payment_credential` check passes.
_SWEEP_SCRIPT = (
    "addr_test1zq3kpwwmyqpppm49huqghuttgda85mkncdps99jne0ad6xed"
    "anvqr0pyy3ne06uvxkaalx8ds4x55z9gq6znqp5p06xqhwh4ht"
)


def _uniform_spend_redeemers(n: int, payload: str = "d87980"):
    return [
        {"validator": {"index": i, "purpose": "spend"},
         "redeemer": payload,
         "executionUnits": {"memory": 600, "cpu": 100}}
        for i in range(n)
    ]


class TestUniformSweepGuard:
    """Owner-sweep fingerprint suppresses the lazy-validator floor.

    The shape (many script inputs, identical spend redeemers, no script
    return) is structurally a UTxO consolidation rather than a
    double-satisfaction exploit. The gate may still fire and the
    weighted score is unchanged, but the structural band floor that
    normally lifts lazy-validator hits into High is suppressed.
    """

    def test_uniform_sweep_suppressed(self, scorer):
        # A uniform sweep (owner consolidating their own script UTxOs) is not
        # double satisfaction; it is now suppressed entirely (no finding, -1),
        # not merely band-capped.
        n = 12  # > min_inputs=10
        inputs = [
            {"address": _SWEEP_SCRIPT, "value": {"lovelace": 2_600_000}}
            for _ in range(n)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 25_000_000}}]
        redeemers = _uniform_spend_redeemers(n)
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.sub_scores["uniform_sweep"] is True
        assert result.sub_scores["s_exunits_inv"] > 0.8
        assert result.score == -1.0

    def test_below_min_inputs_does_not_engage_guard(self, scorer):
        # n=4 is the canonical lazy-validator scenario from the existing
        # floor test; the sweep guard must not engage and the floor
        # behaviour must be preserved.
        n = 4
        inputs = [
            {"address": _SWEEP_SCRIPT, "value": {"lovelace": 2_700_000}}
            for _ in range(n)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 10_000_000}}]
        redeemers = _uniform_spend_redeemers(n)
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert "uniform_script_sweep_guard" not in result.reasons
        assert "lazy_validator_band_floor" in result.reasons

    def test_distinct_redeemer_payloads_do_not_engage_guard(self, scorer):
        n = 12
        inputs = [
            {"address": _SWEEP_SCRIPT, "value": {"lovelace": 2_600_000}}
            for _ in range(n)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 25_000_000}}]
        # Two distinct payloads → not a uniform sweep.
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "redeemer": "d87980" if i % 2 == 0 else "d87a80",
             "executionUnits": {"memory": 600, "cpu": 100}}
            for i in range(n)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert "uniform_script_sweep_guard" not in result.reasons
        assert "lazy_validator_band_floor" in result.reasons

    def test_script_return_disengages_guard(self, scorer):
        # If any output goes back to the same payment credential, this is
        # not a sweep (the script still has state); fall back to normal
        # scoring including the lazy-validator floor.
        n = 12
        inputs = [
            {"address": _SWEEP_SCRIPT, "value": {"lovelace": 2_600_000}}
            for _ in range(n)
        ]
        outputs = [
            {"address": _SWEEP_SCRIPT, "value": {"lovelace": 5_000_000}},
            {"address": WALLET, "value": {"lovelace": 20_000_000}},
        ]
        redeemers = _uniform_spend_redeemers(n)
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert "uniform_script_sweep_guard" not in result.reasons
        assert "lazy_validator_band_floor" in result.reasons


class TestLazyValidatorExtractionGate:
    """The lazy-validator floor must NOT lift state-machine contracts.

    A contract that consumes 2 of its own UTxOs and writes the result
    back to the same script extracts nothing (``s_extraction = 0``).
    Cheap execution per input is normal for a state machine, so the
    floor's lazy-validator predicate is not enough on its own: the
    extraction-min gate gives it the missing "and value left the
    script" semantics that double-satisfaction requires.
    """

    def test_state_machine_with_value_returned_suppressed(self, scorer):
        # 2 inputs from the script, 1 output back to the same script carrying
        # the consolidated value: value returns to the script (state
        # continuation, s_extraction = 0), not extraction. Now suppressed
        # entirely (no finding, -1) rather than scored-and-not-floored.
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
        ]
        outputs = [{"address": SCRIPT, "value": {"lovelace": 9_500_000}}]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "executionUnits": {"memory": 600, "cpu": 100}}
            for i in range(2)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.sub_scores["s_extraction"] == 0.0
        assert result.sub_scores["value_returned_lovelace"] > 0
        assert result.score == -1.0

    def test_floor_still_fires_on_small_extraction(self, scorer):
        # The canonical low-value-drain case the floor exists for: the
        # validator was tricked into approving a small extraction with
        # near-zero CPU. Even a tiny positive s_extraction must keep the
        # floor active so CTF-05-shaped exploits land in High.
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 2_700_000}}
            for _ in range(4)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 10_000_000}}]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "executionUnits": {"memory": 600, "cpu": 100}}
            for i in range(4)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        # Small but strictly positive lovelace extraction signal.
        assert result.sub_scores["s_extraction"] > 0.0
        assert "lazy_validator_band_floor" in result.reasons
        assert result.score >= BAND_HIGH_THRESHOLD


class TestUniformSweepGuardAndAllowlistInteraction:
    """The Moderate cap on the sweep guard must override the allowlist
    reweight.

    Regression for the 14 sweep-cluster alerts that initially fired
    Critical, were dropped to Moderate by ``uniform_script_sweep_guard``,
    then climbed back to High once the same script was added to
    ``allowlist_prefixes.preprod``: the allowlist's reweight path moves
    extraction weight onto ``s_inputs`` (saturated for a 150-input
    sweep), pushing the weighted score above the High threshold. The
    cap inside the sweep-guard branch is what keeps the band at
    Moderate regardless of allowlist path.
    """

    def test_allowlisted_sweep_suppressed(self, scorer, monkeypatch):
        # Inject the sweep script into the preprod allowlist for the
        # duration of this test so the reweight path activates.
        from app.analysis.scorers import multiple_sat as ms_mod
        monkeypatch.setattr(
            ms_mod, "_ALLOWLIST",
            {**ms_mod._ALLOWLIST, "preprod": (_SWEEP_SCRIPT,)},
        )
        n = 12  # > min_inputs=10
        inputs = [
            {"address": _SWEEP_SCRIPT, "value": {"lovelace": 2_600_000}}
            for _ in range(n)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 25_000_000}}]
        redeemers = _uniform_spend_redeemers(n)
        result = scorer.score(_features(inputs, outputs, redeemers))
        # A uniform sweep is suppressed regardless of allowlist status (it can
        # no longer climb back to High via the allowlist reweight, because it
        # never reaches scoring).
        assert result.sub_scores["uniform_sweep"] is True
        assert result.score == -1.0


# A script address that groups consistently by payment credential. Distinct from
# SCRIPT so per-script baselines planted in these tests are unambiguous.
_EXTRACT_SCRIPT = "addr_test1wq3pw00c65cg"


def _two_asset_extraction_features():
    """A CTF-01-shaped double-sat: 2 script inputs each carrying a distinct
    native asset, both assets (and the lovelace) leave to a wallet. Heavy CPU
    per input so the lazy-validator floor does NOT engage (the validator did
    real work) and the score is driven purely by the extraction axis. Not a
    uniform sweep (2 inputs) and nothing returns to the script, so it reaches
    scoring rather than the sweep/return suppression.
    """
    inputs = [
        {"address": _EXTRACT_SCRIPT, "value": {"lovelace": 5_000_000, "pol1": {"nft1": 1}}},
        {"address": _EXTRACT_SCRIPT, "value": {"lovelace": 5_000_000, "pol2": {"nft2": 1}}},
    ]
    outputs = [{"address": WALLET, "value": {
        "lovelace": 9_500_000, "pol1": {"nft1": 1}, "pol2": {"nft2": 1}}}]
    redeemers = [
        {"validator": {"index": i, "purpose": "spend"},
         "executionUnits": {"memory": 5_000_000, "cpu": 10_000_000}}
        for i in range(2)
    ]
    return _features(inputs, outputs, redeemers)


def _plant_baselines(monkeypatch, rows, calls=None):
    """Patch the baseline lookup with a fixed ``(scope_type, feature) -> row`` map.

    ``rows`` values are baseline dicts; absent keys resolve to None (missing).
    Patches the module object that ``normalise.resolve_baseline`` calls, so the
    whole multiple_sat -> scorer_config -> normalise resolve chain sees it.
    """
    from app.analysis import normalise as norm

    def _fn(network, scope_type, scope_id, feature):
        if calls is not None:
            calls.append((scope_type, feature))
        row = rows.get((scope_type, feature))
        return dict(row) if row else None

    monkeypatch.setattr(norm.clickhouse, "get_baseline", _fn)


class TestPerScriptExtractionBaseline:
    """The extraction axis resolves per_script -> bootstrap, never global.

    Established high-volume contracts are de-saturated against their own norm;
    rare/novel scripts (where one-shot double-sat exploits live, e.g. CTF-01)
    stay on the conservative bootstrap. The global tier is never consulted,
    because the global value/asset-extraction distribution is dominated by
    legitimate batchers and would silence detection on rare scripts.
    """

    def test_ctf01_rare_script_stays_on_bootstrap(self, scorer, monkeypatch):
        from app.analysis.normalise import BAND_MODERATE_THRESHOLD
        # No baselines at all -> bootstrap (n_assets p99=2): a 2-asset
        # extraction saturates -> Moderate. This is the CTF-01 recall anchor.
        _plant_baselines(monkeypatch, {})
        result = scorer.score(_two_asset_extraction_features())
        assert result.sub_scores["n_assets_out_of_script"] == 2.0
        assert result.score >= BAND_MODERATE_THRESHOLD
        assert result.baseline_source == "bootstrap"

    def test_global_baseline_ignored_for_extraction(self, scorer, monkeypatch):
        from app.analysis.normalise import BAND_MODERATE_THRESHOLD
        # A usable GLOBAL n_assets baseline (p99=5) exists that WOULD
        # de-saturate a 2-asset extraction to Low if consulted. The per_script
        # restriction must skip it, so the score stays Moderate on bootstrap.
        rows = {("global", "n_assets_out_of_script"):
                {"p50": 1.0, "p99": 5.0, "sample_count": 5000}}
        calls = []
        _plant_baselines(monkeypatch, rows, calls)
        result = scorer.score(_two_asset_extraction_features())
        assert result.score >= BAND_MODERATE_THRESHOLD
        assert result.baseline_source != "global"
        # The regression lock: global was never even queried for the axis.
        assert ("global", "n_assets_out_of_script") not in calls

    def test_per_script_baseline_desaturates_high_volume(self, scorer, monkeypatch):
        from app.analysis.normalise import BAND_MODERATE_THRESHOLD
        # A high-volume contract's own baseline: 2 assets / 10 ADA is its norm,
        # so its routine spend de-saturates below Moderate. A genuine spike
        # above its own p99 would still fire.
        rows = {
            ("per_script", "n_assets_out_of_script"):
                {"p50": 2.0, "p99": 4.0, "sample_count": 300},
            ("per_script", "net_value_out_of_script"):
                {"p50": 10_000_000.0, "p99": 100_000_000.0, "sample_count": 300},
        }
        _plant_baselines(monkeypatch, rows)
        result = scorer.score(_two_asset_extraction_features())
        # Not exactly 0: the anchor-relative p50 bound clamps the learned
        # median toward the bootstrap anchor (recall-positive tightening),
        # so a routine 2-asset spend keeps a small residual signal. It must
        # stay far below the reason threshold and below the Moderate band.
        import app.analysis.scorer_config as sc_mod
        reason_t = float(sc_mod.get("multiple_sat")["reason_threshold"])
        assert result.sub_scores["s_extraction"] < reason_t / 2
        assert result.score < BAND_MODERATE_THRESHOLD
        assert result.baseline_source == "per_script"


def _extraction_features(n_assets, lovelace_in_per_input=2_400_000, cpu=10_000_000):
    """2 script inputs carrying ``n_assets`` distinct native assets that all
    leave to a wallet, so ``n_assets_out == n_assets`` while inputs stay low
    (not a uniform sweep). Lovelace nets ~p50 of the planted net_value baseline,
    so the asset axis drives the score. ``cpu`` per input controls whether the
    lazy-validator floor engages (low cpu -> lazy).
    """
    assets = {f"pol{i}": {f"nft{i}": 1} for i in range(n_assets)}
    inputs = [
        {"address": _EXTRACT_SCRIPT, "value": {"lovelace": lovelace_in_per_input, **assets}},
        {"address": _EXTRACT_SCRIPT, "value": {"lovelace": lovelace_in_per_input}},
    ]
    out_value = {"lovelace": int(lovelace_in_per_input * 2 * 0.95), **assets}
    outputs = [{"address": WALLET, "value": out_value}]
    redeemers = [
        {"validator": {"index": i, "purpose": "spend"},
         "executionUnits": {"memory": 5_000_000, "cpu": cpu}}
        for i in range(2)
    ]
    return _features(inputs, outputs, redeemers)


# Established-contract baseline: normal extraction is 2-3 assets / ~4.8-9.6 ADA.
_EST_BASELINES = {
    ("per_script", "n_assets_out_of_script"): {"p50": 2.0, "p99": 3.0, "sample_count": 1893},
    ("per_script", "net_value_out_of_script"): {"p50": 4_800_000.0, "p99": 9_600_000.0, "sample_count": 1893},
}


class TestPerScriptExtractionHeadroom:
    """Per-script extraction anchors get headroom so an established contract's
    normal upper-range extraction (its common p99 value) does not saturate;
    rare/novel scripts on the bootstrap anchor stay conservative (CTF-01 recall).
    """

    def test_per_script_normal_upper_desaturates(self, scorer, monkeypatch):
        from app.analysis.normalise import BAND_MODERATE_THRESHOLD
        _plant_baselines(monkeypatch, _EST_BASELINES)
        # 3 assets == the contract's p99 (its common upper-normal value). With
        # headroom (anchor 2 + (3-2)*3 = 5) this no longer saturates.
        result = scorer.score(_extraction_features(3))
        assert result.sub_scores["s_extraction_assets"] < 1.0
        assert result.score < BAND_MODERATE_THRESHOLD   # Informational, not an alert
        assert result.baseline_source == "per_script"

    def test_per_script_anomaly_still_fires(self, scorer, monkeypatch):
        from app.analysis.normalise import BAND_MODERATE_THRESHOLD
        _plant_baselines(monkeypatch, _EST_BASELINES)
        # 8 assets is well above the contract's norm (p99=3) -> saturates -> fires.
        result = scorer.score(_extraction_features(8))
        assert result.sub_scores["s_extraction_assets"] == 1.0
        assert result.score >= BAND_MODERATE_THRESHOLD

    def test_bootstrap_unaffected_by_headroom(self, scorer, monkeypatch):
        from app.analysis.normalise import BAND_MODERATE_THRESHOLD
        # No per-script baseline -> bootstrap (n_assets p99=2). The 2-asset
        # CTF-01 shape must still saturate; headroom must NOT touch bootstrap.
        _plant_baselines(monkeypatch, {})
        result = scorer.score(_extraction_features(2))
        assert result.sub_scores["s_extraction"] == 1.0
        assert result.score >= BAND_MODERATE_THRESHOLD
        assert result.baseline_source == "bootstrap"

    def test_lazy_validator_floor_independent_of_headroom(self, scorer, monkeypatch):
        # Per-script baseline + near-zero CPU (lazy validator) + extraction: the
        # floor must still fire to High, because its gate uses the un-widened
        # extraction (headroom must not weaken the high-confidence path).
        _plant_baselines(monkeypatch, _EST_BASELINES)
        result = scorer.score(_extraction_features(3, cpu=1))
        assert "lazy_validator_band_floor" in result.reasons
        assert result.score >= BAND_HIGH_THRESHOLD


_NFT_POLICY = "c" * 56


class TestSuppressionEscape:
    """Extraction-magnitude escape hatch (multiple_sat.suppression_escape).

    The two benign-shape suppressions are attacker-reachable: returning 1
    lovelace to the script forces the state-continuation arm, and a large
    identical-redeemer full drain matches the sweep fingerprint. When the
    un-widened extraction floor signal exceeds the threshold, the finding
    must surface at Moderate instead of being silenced to -1.
    """

    def _nft(self, i):
        return {_NFT_POLICY: {f"{i:02d}" * 4: 1}}

    def test_return_one_lovelace_double_sat_not_silenced(self, scorer):
        # 2 script inputs each holding a distinct NFT; the attacker drains
        # both NFTs to their wallet, runs a REAL (non-lazy) validator, and
        # returns exactly 1 lovelace to the script to trigger the
        # state-continuation suppression. Previously: no finding (-1).
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000, **self._nft(i)}}
            for i in range(2)
        ]
        outputs = [
            {"address": WALLET, "value": {
                "lovelace": 9_500_000,
                _NFT_POLICY: {("00" * 4): 1, ("01" * 4): 1},
            }},
            {"address": SCRIPT, "value": {"lovelace": 1}},
        ]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "redeemer": f"payload{i}",
             "executionUnits": {"memory": 600, "cpu": 9_000_000}}
            for i in range(2)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        # Not lazy (real CPU), so the floor does not apply; the escape must.
        assert result.sub_scores["s_exunits_inv"] < 0.8
        assert result.score != -1.0
        # Moderate band, surfaced for review; capped, never High on this shape.
        assert BAND_MODERATE_THRESHOLD <= result.score <= BAND_MODERATE_MAX
        assert "extraction_escape_moderate_cap" in result.reasons

    def test_uniform_full_drain_double_sat_not_silenced(self, scorer):
        # 12 identical-redeemer inputs each holding a distinct NFT, full
        # drain to the attacker wallet: matches the sweep fingerprint
        # exactly, but the asset axis saturates (12 >> p99=2), so the
        # escape keeps the finding at the top of Moderate.
        n = 12
        inputs = [
            {"address": _SWEEP_SCRIPT,
             "value": {"lovelace": 2_600_000, **self._nft(i)}}
            for i in range(n)
        ]
        outputs = [
            {"address": WALLET, "value": {
                "lovelace": 31_000_000,
                _NFT_POLICY: {f"{i:02d}" * 4: 1 for i in range(n)},
            }},
        ]
        redeemers = _uniform_spend_redeemers(n)
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.score != -1.0
        assert result.score == BAND_MODERATE_MAX
        assert "uniform_script_sweep_guard" in result.reasons
        assert "extraction_escape_moderate_cap" in result.reasons

    def test_small_sweep_below_escape_floor_still_suppressed(self, scorer):
        # Lovelace-only sweep far below the escape threshold (31.2M against
        # the 5M/500M bootstrap anchor ~= 0.053): the benign suppression
        # must keep winning.
        n = 12
        inputs = [
            {"address": _SWEEP_SCRIPT, "value": {"lovelace": 2_600_000}}
            for _ in range(n)
        ]
        outputs = [{"address": WALLET, "value": {"lovelace": 31_000_000}}]
        redeemers = _uniform_spend_redeemers(n)
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.score == -1.0

    def test_single_nft_drain_one_lovelace_return_not_silenced(self, scorer):
        # ATTACK-MUST-FIRE boundary case: one NFT drained, 1 lovelace
        # returned to the script, real (non-lazy) validator. The asset
        # floor signal is normalise(1, p50=0, p99=2) = 1/(2+EPSILON)
        # ~= 0.49999975, a hair BELOW the old 0.5 threshold, so the
        # strict > comparison silenced exactly this single-NFT drain.
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
            {"address": SCRIPT,
             "value": {"lovelace": 5_000_000, **self._nft(0)}},
        ]
        outputs = [
            {"address": WALLET, "value": {
                "lovelace": 9_500_000,
                _NFT_POLICY: {("00" * 4): 1},
            }},
            {"address": SCRIPT, "value": {"lovelace": 1}},
        ]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "redeemer": f"payload{i}",
             "executionUnits": {"memory": 600, "cpu": 9_000_000}}
            for i in range(2)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.sub_scores["s_exunits_inv"] < 0.8  # not lazy
        assert result.score != -1.0
        # Exactly the Moderate band: floored at its bottom, capped at its top.
        assert BAND_MODERATE_THRESHOLD <= result.score <= BAND_MODERATE_MAX
        assert "extraction_escape_moderate_cap" in result.reasons

    def test_small_nft_drain_under_capped_poisoned_baseline_not_silenced(
        self, scorer, monkeypatch
    ):
        # ATTACK-MUST-FIRE under poisoning: a per-script n_assets baseline
        # poisoned wide is capped at 5x the bootstrap anchor (p99=10), so a
        # 4-NFT drain floors at normalise(4, 2, 10) = 0.25 >= 0.10 and the
        # escape fires; with the old 0.5 threshold it was silenced.
        _plant_baselines(monkeypatch, {
            ("per_script", "n_assets_out_of_script"): {
                "p50": 2.0, "p99": 1e6, "sample_count": 500,
                "computed_at": None, "window_days": 90,
            },
        })
        n = 4
        inputs = [
            {"address": SCRIPT,
             "value": {"lovelace": 2_700_000, **self._nft(i)}}
            for i in range(n)
        ]
        outputs = [
            {"address": WALLET, "value": {
                "lovelace": 10_000_000,
                _NFT_POLICY: {f"{i:02d}" * 4: 1 for i in range(n)},
            }},
            {"address": SCRIPT, "value": {"lovelace": 1}},
        ]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "redeemer": f"payload{i}",
             "executionUnits": {"memory": 600, "cpu": 9_000_000}}
            for i in range(n)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.score != -1.0
        assert BAND_MODERATE_THRESHOLD <= result.score <= BAND_MODERATE_MAX
        assert "extraction_escape_moderate_cap" in result.reasons

    def test_in_bound_median_poisoned_drain_not_silenced(self, scorer, monkeypatch):
        # ATTACK-MUST-FIRE (p50-bound review fix): a per-script n_assets
        # baseline median-poisoned to exactly the OLD cap-relative p50
        # bound, cap / (1 + min_spread_ratio) (~9.09 with shipped knobs),
        # used to resolve unchanged, so a drain of any asset count below it
        # normalised to 0 on the asset axis and the suppression escape
        # never fired: an in-bound poisoned drain was silenced. The
        # anchor-relative bound (per_script_p50_cap_spread_fraction) clamps
        # the resolved median to anchor_p50 + K * anchor spread, so the
        # same drain must now escape to Moderate. Every value derives from
        # the config knobs, not bare literals.
        import math
        import app.analysis.scorer_config as sc_mod
        from app.analysis.normalise import normalise

        boot = sc_mod.get("multiple_sat")["bootstrap_anchors"]
        anchor_p50, anchor_p99 = sc_mod.anchor(boot, "n_assets_out_of_script")
        cap = sc_mod._P99_CAP_MULTIPLIER * anchor_p99
        old_bound = cap / (1.0 + sc_mod._MIN_SPREAD_RATIO)
        # The drain sits between the NEW p50 bound and the old one (and
        # under the p99 cap): the silenced-yesterday, must-fire-today case.
        n_drain = math.floor(old_bound)
        new_bound = anchor_p50 + sc_mod._P50_CAP_SPREAD_FRACTION * (
            anchor_p99 - anchor_p50
        )
        assert new_bound < n_drain < cap
        # Regression statement: under the old resolved pair this exact
        # drain normalised to 0 on the asset axis (silenced).
        assert normalise(n_drain, p50=old_bound, p99=cap) == 0.0

        _plant_baselines(monkeypatch, {
            ("per_script", "n_assets_out_of_script"): {
                "p50": old_bound, "p99": cap * 1e5, "sample_count": 500,
                "computed_at": None, "window_days": 90,
            },
        })
        inputs = [
            {"address": SCRIPT,
             "value": {"lovelace": 2_700_000, **self._nft(i)}}
            for i in range(n_drain)
        ]
        outputs = [
            {"address": WALLET, "value": {
                "lovelace": 2_700_000 * n_drain - 300_000,
                _NFT_POLICY: {f"{i:02d}" * 4: 1 for i in range(n_drain)},
            }},
            {"address": SCRIPT, "value": {"lovelace": 1}},
        ]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "redeemer": f"payload{i}",
             "executionUnits": {"memory": 600, "cpu": 9_000_000}}
            for i in range(n_drain)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.sub_scores["s_exunits_inv"] < 0.8  # not lazy
        assert result.score != -1.0
        assert BAND_MODERATE_THRESHOLD <= result.score <= BAND_MODERATE_MAX
        assert "extraction_escape_moderate_cap" in result.reasons

    def test_escape_fires_at_exact_threshold(self):
        # Structural boundary pin for >= : a floor signal exactly AT the
        # configured threshold must escape, not suppress.
        import app.analysis.scorers.multiple_sat as ms_mod

        axes = ms_mod._ScriptAxes(
            lovelace_in=1, lovelace_out=1, net_value=0, n_assets_out=0,
            exunits_per_input=9e6, s_extraction_lov=0.0,
            s_extraction_assets=0.0, s_extraction=0.0,
            s_extraction_floor=ms_mod._SUPP_ESCAPE_FLOOR_MIN,
            s_exunits_inv=0.0, s_inputs=0.0, s_recurrence=0.0,
            bl_source="bootstrap",
        )
        suppressed, escaped = ms_mod._suppression_outcome(
            axes, allowlisted=False, uniform_sweep=False, floored=False,
        )
        assert (suppressed, escaped) == (False, True)

    def test_escaped_finding_always_bands_moderate(self, scorer, monkeypatch):
        # Structural (not fixture-empirical): WHATEVER the weighted score,
        # an escaped finding must band exactly Moderate: the floor closes
        # the hole where a small weighted sum landed in Informational
        # (no action) despite the escape deliberately un-silencing it.
        import app.analysis.scorers.multiple_sat as ms_mod
        from app.analysis.normalise import score_to_band

        monkeypatch.setattr(
            ms_mod, "_suppression_outcome", lambda *a, **k: (False, True),
        )
        # Low-extraction state-continuation shape: tiny weighted score.
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}}
            for _ in range(2)
        ]
        outputs = [
            {"address": WALLET, "value": {"lovelace": 100_000}},
            {"address": SCRIPT, "value": {"lovelace": 9_700_000}},
        ]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "redeemer": f"payload{i}",
             "executionUnits": {"memory": 600, "cpu": 9_000_000}}
            for i in range(2)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert BAND_MODERATE_THRESHOLD <= result.score <= BAND_MODERATE_MAX
        assert score_to_band(result.score) == "Moderate"


class TestBaselinePoisoningResistance:
    """A pre-trained (poisoned) wide per-script extraction baseline must not
    silence a real drain: the p99 cap in resolved_or_bootstrap bounds the
    saturation point at K x the bootstrap anchor, so the lazy-validator
    floor still fires."""

    def test_poisoned_wide_baseline_cannot_silence_drain(self, scorer, monkeypatch):
        poisoned = {
            # Wide spread (passes the min-spread usability guard), huge p99.
            ("per_script", "net_value_out_of_script"): {
                "p50": 5_000_000.0, "p99": 1e15, "sample_count": 500,
                "computed_at": None, "window_days": 90,
            },
            ("per_script", "n_assets_out_of_script"): {
                "p50": 0.0, "p99": 1000.0, "sample_count": 500,
                "computed_at": None, "window_days": 90,
            },
        }
        _plant_baselines(monkeypatch, poisoned)
        n = 4
        nft_policy = "d" * 56
        inputs = [
            {"address": SCRIPT,
             "value": {"lovelace": 2_700_000,
                       nft_policy: {f"{i:02d}" * 4: 1 for i in range(i * 3, i * 3 + 3)}}}
            for i in range(n)
        ]
        outputs = [{
            "address": WALLET,
            "value": {"lovelace": 10_000_000,
                      nft_policy: {f"{i:02d}" * 4: 1 for i in range(12)}},
        }]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "redeemer": f"p{i}",
             "executionUnits": {"memory": 600, "cpu": 100}}
            for i in range(n)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        # 12 distinct NFTs drained vs capped p99 (5 x bootstrap 2 = 10):
        # extraction saturates despite the poisoned baseline, and the
        # lazy-validator floor lands the score in High.
        assert result.score >= BAND_HIGH_THRESHOLD
        assert "lazy_validator_band_floor" in result.reasons

    def test_median_poisoned_baseline_cannot_silence_drain(self, scorer, monkeypatch):
        # ATTACK-MUST-FIRE for the p50 vector: normalise() subtracts p50
        # first, so a poisoned MEDIAN (p50=500 assets, p99=1000) made every
        # real drain normalise to 0; the p99 cap alone never touched it.
        # With the p50 bound (derived from the capped p99), the axis stays
        # alive and the lazy-validator floor fires.
        poisoned = {
            ("per_script", "net_value_out_of_script"): {
                "p50": 1e14, "p99": 1e15, "sample_count": 500,
                "computed_at": None, "window_days": 90,
            },
            ("per_script", "n_assets_out_of_script"): {
                "p50": 500.0, "p99": 1000.0, "sample_count": 500,
                "computed_at": None, "window_days": 90,
            },
        }
        _plant_baselines(monkeypatch, poisoned)
        n = 4
        nft_policy = "d" * 56
        inputs = [
            {"address": SCRIPT,
             "value": {"lovelace": 2_700_000,
                       nft_policy: {f"{i:02d}" * 4: 1 for i in range(i * 3, i * 3 + 3)}}}
            for i in range(n)
        ]
        outputs = [
            {"address": WALLET,
             "value": {"lovelace": 10_000_000,
                       nft_policy: {f"{i:02d}" * 4: 1 for i in range(12)}}},
            {"address": SCRIPT, "value": {"lovelace": 1}},
        ]
        redeemers = [
            {"validator": {"index": i, "purpose": "spend"},
             "redeemer": f"p{i}",
             "executionUnits": {"memory": 600, "cpu": 100}}
            for i in range(n)
        ]
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.score >= BAND_HIGH_THRESHOLD
        assert "lazy_validator_band_floor" in result.reasons
