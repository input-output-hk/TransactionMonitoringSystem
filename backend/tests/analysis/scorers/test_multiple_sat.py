"""Unit tests for the Multiple Satisfaction scorer (Class 4)."""

import pytest
from app.analysis.scorers.multiple_sat import MultipleSatScorer


@pytest.fixture
def scorer():
    return MultipleSatScorer()


SCRIPT = "addr_test1wz5fxvalex"
WALLET = "addr_test1qz5fxvalex"


def _features(inputs, outputs=None, redeemers=None):
    return {
        "tx_hash": "ms01",
        "network": "preprod",
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
    def test_low_redeemer_ratio_scores_high(self, scorer):
        """3 inputs from same script, only 1 redeemer: ratio ~ 0.33, inverted ~ 0.67."""
        inputs = [{"address": SCRIPT, "value": {"lovelace": 10_000_000}} for _ in range(3)]
        # Ogmios v6 dict format: only spend:0 present for 3 script inputs
        redeemers = {
            "spend:0": {"executionUnits": {"memory": 100000, "cpu": 200000}},
        }
        result = scorer.score(_features(inputs, redeemers=redeemers))
        assert result.sub_scores["redeemer_input_ratio_inv"] > 0.3
        assert result.score > 20

    def test_full_redeemer_coverage_low_score(self, scorer):
        """2 inputs, 2 redeemers: ratio = 1.0, inverted = 0.0."""
        inputs = [{"address": SCRIPT, "value": {"lovelace": 5_000_000}} for _ in range(2)]
        redeemers = {
            "spend:0": {"executionUnits": {"memory": 100000, "cpu": 200000}},
            "spend:1": {"executionUnits": {"memory": 100000, "cpu": 200000}},
        }
        result = scorer.score(_features(inputs, redeemers=redeemers))
        assert result.sub_scores["redeemer_input_ratio_inv"] < 0.1

    def test_value_extraction_boosts_score(self, scorer):
        """Large net value leaving script should boost net_value_extraction."""
        inputs = [
            {"address": SCRIPT, "value": {"lovelace": 100_000_000}},
            {"address": SCRIPT, "value": {"lovelace": 100_000_000}},
        ]
        outputs = [
            {"address": WALLET, "value": {"lovelace": 195_000_000}},  # extraction
            {"address": SCRIPT, "value": {"lovelace": 2_000_000}},    # small return
        ]
        redeemers = {"spend:0": {"executionUnits": {"memory": 50000, "cpu": 100000}}}
        result = scorer.score(_features(inputs, outputs, redeemers))
        assert result.sub_scores["net_value_extraction"] > 0.3

    def test_batch_script_allowlisted(self, scorer):
        """Known batch-processing scripts should be skipped."""
        # SundaeSwap v3 batch validator prefix
        batch_addr = "addr1w9zsmyfc5tg49ng9gqaetm8qheyheemxakq47x7qfwnq5wq_full"
        inputs = [{"address": batch_addr, "value": {"lovelace": 5_000_000}} for _ in range(3)]
        result = scorer.score(_features(inputs))
        assert result.score == 0.0
