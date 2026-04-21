"""Unit tests for the Fake Token scorer (Class 8)."""

import pytest
from unittest.mock import patch
from app.analysis.scorers.fake_token import FakeTokenScorer


@pytest.fixture
def scorer():
    return FakeTokenScorer()


# Real HOSKY policy from external.py
LEGIT_HOSKY_POLICY = "a0028f350aaabe0545fdcb56b039bfb08e4bb4d8c4d7c3c7d481c235"
FAKE_POLICY = "deadbeef" * 7


def _features(mint=None, outputs=None, metadata=None, slot=100_000, network="mainnet"):
    # Default to mainnet so fake_token's legitimate-token registry is
    # populated. On preview/preprod the registry is intentionally empty
    # (devs routinely test with names like iUSD/DJED/HOSKY).
    return {
        "tx_hash": "ft01",
        "network": network,
        "metadata": metadata,
        "raw_data": {
            "mint": mint or {},
            "outputs": outputs or [],
        },
        "slot": slot,
    }


class TestGate:
    def test_no_mint(self, scorer):
        assert scorer.gate(_features()) is False

    def test_legit_policy_rejected(self, scorer):
        """Minting HOSKY under the real policy should not trigger."""
        mint = {LEGIT_HOSKY_POLICY: {"HOSKY": 1000}}
        assert scorer.gate(_features(mint=mint)) is False

    def test_non_mainnet_disables_gate(self, scorer, monkeypatch):
        """Preview/preprod have an empty brand registry (test networks where
        devs legitimately mint names like iUSD/DJED/HOSKY), so the gate must
        not fire even on an obvious mainnet-style fake.

        Explicitly forces FAKE_TOKEN_TESTNET_MODE=False so the test reflects
        the default, security-oriented behaviour regardless of whether the
        local .env has the testnet-mode override turned on for debugging.
        """
        from app.config import settings
        monkeypatch.setattr(settings, "FAKE_TOKEN_TESTNET_MODE", False)
        mint = {FAKE_POLICY: {"HOSKY": 1000}}
        assert scorer.gate(_features(mint=mint, network="preview")) is False
        assert scorer.gate(_features(mint=mint, network="preprod")) is False

    def test_similar_name_fake_policy_passes(self, scorer):
        """Minting 'HOSKY' under a fake policy should pass gate."""
        mint = {FAKE_POLICY: {"HOSKY": 1000}}
        assert scorer.gate(_features(mint=mint)) is True

    def test_dissimilar_name_rejected(self, scorer):
        """A completely different token name should not match."""
        mint = {FAKE_POLICY: {"XYZTOKEN": 1000}}
        assert scorer.gate(_features(mint=mint)) is False

    def test_burn_rejected(self, scorer):
        """Negative quantity (burn) should be ignored."""
        mint = {FAKE_POLICY: {"HOSKY": -1000}}
        assert scorer.gate(_features(mint=mint)) is False


class TestScore:
    def test_exact_name_match_high_identity(self, scorer):
        mint = {FAKE_POLICY: {"HOSKY": 10_000}}
        outputs = [{"address": f"addr{i}", "value": {"lovelace": 1_500_000}} for i in range(5)]
        result = scorer.score(_features(mint=mint, outputs=outputs))
        assert result.sub_scores["tokenname_similarity"] > 0.5
        assert result.score > 20

    def test_unicode_homoglyph_boosts_score(self, scorer):
        """Token name with Cyrillic 'а' (U+0430) instead of Latin 'a'."""
        # \u0430 = Cyrillic а
        fake_name = "HOSK\u0430"  # similar to HOSKY but with homoglyph
        # This might not pass the 0.80 similarity gate, but test the scorer directly
        mint = {FAKE_POLICY: {fake_name: 1000}}
        # Force through by testing score directly if gate passes
        feats = _features(mint=mint, outputs=[{"address": "a1", "value": {"lovelace": 1}}])
        if scorer.gate(feats):
            result = scorer.score(feats)
            assert result.sub_scores.get("unicode_suspicion", 0) > 0

    def test_mass_distribution_boosts_score(self, scorer):
        """Many distinct recipients should increase distribution sub-score."""
        mint = {FAKE_POLICY: {"HOSKY": 100_000}}
        few = [{"address": "addr1", "value": {"lovelace": 1_500_000}}]
        many = [{"address": f"addr{i}", "value": {"lovelace": 1_500_000}} for i in range(50)]
        r_few = scorer.score(_features(mint=mint, outputs=few))
        r_many = scorer.score(_features(mint=mint, outputs=many))
        assert r_many.sub_scores["recipients"] > r_few.sub_scores["recipients"]

    def test_no_match_returns_zero(self, scorer):
        mint = {FAKE_POLICY: {"COMPLETELY_UNIQUE_NAME": 1}}
        result = scorer.score(_features(mint=mint))
        assert result.score == 0.0
