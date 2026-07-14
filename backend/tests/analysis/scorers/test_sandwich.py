"""Unit tests for the Sandwich scorer (Class 6)."""

import pytest

from app.analysis.scorers.sandwich import SandwichScorer


@pytest.fixture
def scorer():
    return SandwichScorer()


def _features(sandwich=None):
    return {
        "tx_hash": "sw01",
        "network": "preprod",
        "raw_data": {},
        "sandwich": sandwich,
    }


class TestGate:
    def test_no_data(self, scorer):
        assert scorer.gate(_features()) is False

    def test_slot_span_too_large(self, scorer):
        assert scorer.gate(_features(sandwich={"slot_span": 10})) is False

    def test_within_window_passes(self, scorer):
        assert scorer.gate(_features(sandwich={"slot_span": 3})) is True


class TestScore:
    def test_linked_attacker_high_score(self, scorer):
        sw = {
            "tx_a": "a01",
            "tx_b": "b01",
            "pool_id": "pool01",
            "asset_pair": "ADA/HOSKY",
            "attacker_linked": True,
            "swap_rate_victim": 0.85,
            "swap_rate_baseline": 1.0,
            "price_impact_a": 0.03,
            "profit_b": 1_000_000,
            "attacker_sandwich_count": 4,
            "slot_span": 2,
        }
        result = scorer.score(_features(sandwich=sw))
        assert result.score > 30
        assert result.sub_scores["attacker_link"] == 1.0

    def test_unlinked_attacker_lower(self, scorer):
        sw = {
            "tx_a": "a01",
            "tx_b": "b01",
            "pool_id": "pool01",
            "asset_pair": "ADA/MIN",
            "attacker_linked": False,
            "swap_rate_victim": 0.95,
            "swap_rate_baseline": 1.0,
            "price_impact_a": 0.01,
            "profit_b": 500_000,
            "attacker_sandwich_count": 1,
            "slot_span": 4,
        }
        result = scorer.score(_features(sandwich=sw))
        assert result.sub_scores["attacker_link"] == 0.2

    def test_low_profit_suppressed(self, scorer):
        """Below the profit floor the candidate is suppressed entirely (score
        -1, no finding), not band-capped: a triple that extracts no material
        ADA is not a sandwich. Even with every structural signal saturated."""
        sw = {
            "tx_a": "a01",
            "tx_b": "b01",
            "pool_id": "pool01",
            "asset_pair": "ADA/SNEK",
            "attacker_linked": True,
            "swap_rate_victim": 0.50,
            "swap_rate_baseline": 1.0,
            "price_impact_a": 0.10,
            "profit_b": 100_000,  # below 200,000 floor
            "attacker_sandwich_count": 10,
            "slot_span": 1,
        }
        result = scorer.score(_features(sandwich=sw))
        assert result.score == -1.0
        # Observability retained: the raw profit that drove suppression.
        assert result.sub_scores["attacker_profit_lovelace"] == 100_000

    def test_zero_profit_structural_match_suppressed(self, scorer):
        """The dominant former false positive: a structural triple with no
        computed profit (the pre-fix default). Must yield no finding."""
        sw = {
            "tx_a": "a01",
            "tx_b": "b01",
            "pool_id": "pool01",
            "asset_pair": "unknown",
            "attacker_linked": True,
            "swap_rate_victim": 0.0,
            "swap_rate_baseline": 0.0,
            "price_impact_a": 0.0,
            "profit_b": 0.0,
            "attacker_sandwich_count": 0,
            "slot_span": 2,
        }
        result = scorer.score(_features(sandwich=sw))
        assert result.score == -1.0

    def test_no_data_returns_zero(self, scorer):
        result = scorer.score(_features())
        assert result.score == 0.0
