"""Unit tests for the Token Dust scorer (Class 1)."""

import pytest
from app.analysis.scorers.token_dust import TokenDustScorer


@pytest.fixture
def scorer():
    return TokenDustScorer()


def _make_output(address, lovelace=2_000_000, policies=None):
    """Build a single output dict."""
    value = {"lovelace": lovelace}
    if policies:
        for pid, assets in policies.items():
            value[pid] = assets
    return {"address": address, "value": value}


def _features(outputs):
    return {
        "tx_hash": "dust01",
        "network": "preprod",
        "raw_data": {"outputs": outputs},
    }


SCRIPT_ADDR = "addr_test1wz5fxvalex"
WALLET_ADDR = "addr_test1qz5fxvalex"


class TestGate:
    def test_no_raw_data(self, scorer):
        assert scorer.gate({"raw_data": None}) is False

    def test_wallet_address_rejected(self, scorer):
        out = _make_output(WALLET_ADDR, policies={"policy1": {"tokenA": 1}})
        assert scorer.gate(_features([out])) is False

    def test_script_no_assets_rejected(self, scorer):
        out = _make_output(SCRIPT_ADDR)  # lovelace only
        assert scorer.gate(_features([out])) is False

    def test_script_with_single_asset_rejected(self, scorer):
        # A single-asset output cannot bloat the Value field's CBOR; the gate
        # requires >= min_token_count (default 2) live assets to engage.
        out = _make_output(SCRIPT_ADDR, policies={"policyA": {"tok1": 1}})
        assert scorer.gate(_features([out])) is False

    def test_script_with_bundle_passes(self, scorer):
        out = _make_output(
            SCRIPT_ADDR,
            policies={"policyA": {"tok1": 1, "tok2": 1}},
        )
        assert scorer.gate(_features([out])) is True


class TestScore:
    def test_many_assets_high_score(self, scorer):
        """20 distinct tokens from 5 policies should score high."""
        policies = {}
        for i in range(5):
            policies[f"policy{i:02d}"] = {f"token{j}": 1 for j in range(4)}
        out = _make_output(SCRIPT_ADDR, lovelace=1_500_000, policies=policies)
        result = scorer.score(_features([out]))
        assert result.score > 40
        assert result.sub_scores["unique_assetclass_count"] > 0.5

    def test_single_asset_low_score(self, scorer):
        out = _make_output(SCRIPT_ADDR, lovelace=10_000_000, policies={"p": {"t": 100}})
        result = scorer.score(_features([out]))
        assert result.score < 30

    def test_low_ada_boosts_score(self, scorer):
        """Minimum ADA with many assets: inverted ADA sub-score should be high."""
        policies = {f"p{i}": {f"t{j}": 1 for j in range(3)} for i in range(3)}
        out = _make_output(SCRIPT_ADDR, lovelace=1_200_000, policies=policies)
        result = scorer.score(_features([out]))
        assert result.sub_scores["lovelace_inverted"] > 0.5

    def test_max_across_outputs(self, scorer):
        """Score should be the max across multiple eligible outputs."""
        low = _make_output(SCRIPT_ADDR, lovelace=50_000_000, policies={"p": {"t": 1}})
        high = _make_output(
            SCRIPT_ADDR, lovelace=1_200_000,
            policies={f"p{i}": {f"t{j}": 1 for j in range(5)} for i in range(4)},
        )
        result = scorer.score(_features([low, high]))
        single = scorer.score(_features([high]))
        assert result.score == single.score
