"""Unit tests for the Large Value scorer (Class 2)."""

import pytest
from app.analysis.scorers.large_value import LargeValueScorer


@pytest.fixture
def scorer():
    return LargeValueScorer()


SCRIPT = "addr_test1wz5fxvalex"
WALLET = "addr_test1qz5fxvalex"


def _out(address, lovelace=2_000_000, policies=None):
    value = {"lovelace": lovelace}
    if policies:
        value.update(policies)
    return {"address": address, "value": value}


def _features(outputs):
    return {"tx_hash": "lv01", "network": "preprod", "raw_data": {"outputs": outputs}}


class TestGate:
    def test_wallet_rejected(self, scorer):
        out = _out(WALLET, policies={"p": {"t": 1}})
        assert scorer.gate(_features([out])) is False

    def test_no_assets_rejected(self, scorer):
        out = _out(SCRIPT)
        assert scorer.gate(_features([out])) is False

    def test_too_many_assets_rejected(self, scorer):
        """More than 2 unique asset classes should be rejected (token_dust territory)."""
        out = _out(SCRIPT, policies={"p1": {"a": 1, "b": 1, "c": 1}})
        assert scorer.gate(_features([out])) is False

    def test_single_asset_passes(self, scorer):
        out = _out(SCRIPT, policies={"p": {"t": 10**18}})
        assert scorer.gate(_features([out])) is True

    def test_two_assets_passes(self, scorer):
        out = _out(SCRIPT, policies={"p": {"a": 1, "b": 1}})
        assert scorer.gate(_features([out])) is True


class TestScore:
    def test_extreme_quantity_high_score(self, scorer):
        """10^35 quantity should produce high quantity_digits sub-score."""
        out = _out(SCRIPT, lovelace=1_500_000, policies={"p": {"t": 10**35}})
        result = scorer.score(_features([out]))
        assert result.sub_scores["quantity_digits"] > 0.5
        assert result.score > 30

    def test_normal_quantity_low_score(self, scorer):
        out = _out(SCRIPT, lovelace=5_000_000, policies={"p": {"t": 1000}})
        result = scorer.score(_features([out]))
        assert result.score < 30

    def test_sub_scores_present(self, scorer):
        out = _out(SCRIPT, policies={"p": {"t": 10**20}})
        result = scorer.score(_features([out]))
        for key in ("quantity_digits", "value_cbor_bytes", "lovelace_inverted"):
            assert key in result.sub_scores
