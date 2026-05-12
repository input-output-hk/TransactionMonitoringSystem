"""Unit tests for the Multiple Satisfaction scorer (Class 4)."""

import pytest
from app.analysis.scorers.multiple_sat import (
    MultipleSatScorer,
    _W as _WEIGHTS,
    _compute_n_assets_out,
    _iter_assets,
    _reweight_without_extraction,
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


def _features(inputs, outputs=None, redeemers=None, sender_recurrence=0.0):
    return {
        "tx_hash": "ms01",
        "network": "preprod",
        "sender_recurrence": sender_recurrence,
        "raw_data": {
            "inputs": inputs,
            "outputs": outputs or [],
            "redeemers": redeemers,
        },
    }


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
        result = scorer.score(_features(inputs, outputs, redeemers))
        # s_extraction forced to 0 by allowlist reweight
        assert result.sub_scores["s_extraction"] == 0.0
        assert "allowlisted_batch_script" in result.reasons

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
        r_allow = scorer.score(_features(inputs_allow, outputs_allow, redeemers))
        r_non = scorer.score(_features(inputs_non, outputs_non, redeemers))
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
        assert result.score >= 60.0
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
        assert result.score < 60.0
        assert "lazy_validator_band_floor" not in result.reasons

    def test_floor_does_not_apply_to_allowlisted_scripts(self, scorer):
        # Legitimate batchers run minimal per-input CPU by design (e.g. a
        # DEX settlement script that aggregates orders); the floor must not
        # punish them just because s_exunits_inv saturates.
        from app.analysis.scorers.multiple_sat import _ALLOWLIST
        allowlisted_addr = _ALLOWLIST[0]
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
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert "allowlisted_batch_script" in result.reasons
        assert "lazy_validator_band_floor" not in result.reasons
        assert result.score < 60.0


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
