"""Unit tests for the Multiple Satisfaction scorer (Class 4)."""

import pytest
from app.analysis.scorers.multiple_sat import (
    MultipleSatScorer,
    _W as _WEIGHTS,
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

    def test_two_script_inputs_passes(self, scorer):
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
        ]
        assert scorer.gate(_features(inputs)) is True


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


class TestWeights:
    def test_weights_sum_to_one(self):
        total = _W_EXTRACTION + _W_EXUNITS + _W_INPUTS + _W_RECURRENCE
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_weight_values_match_example_yaml(self):
        """Spec-weight regression guard: the tracked example YAML must always
        carry the Polimi §4.4.3 default weights, regardless of any local
        detection.yaml override the developer might have."""
        import pathlib
        import yaml

        here = pathlib.Path(__file__).resolve()
        example = next(
            p for p in here.parents
            if (p / "config" / "detection.example.yaml").exists()
        ) / "config" / "detection.example.yaml"
        with open(example, encoding="utf-8") as f:
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
