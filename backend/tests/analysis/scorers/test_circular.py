"""Unit tests for the Circular Transfers scorer (Class 7)."""

import pytest
from app.analysis.scorers.circular import CircularScorer


@pytest.fixture
def scorer():
    return CircularScorer()


def _features(cycle=None):
    return {
        "tx_hash": "ci01",
        "network": "preprod",
        "raw_data": {},
        "cycle": cycle,
    }


class TestGate:
    def test_no_cycle_data(self, scorer):
        assert scorer.gate(_features()) is False

    def test_cycle_too_short(self, scorer):
        assert scorer.gate(_features(cycle={"cycle_length": 1, "net_loss_ratio": 0.01})) is False

    def test_cycle_too_long(self, scorer):
        assert scorer.gate(_features(cycle={"cycle_length": 7, "net_loss_ratio": 0.01})) is False

    def test_high_net_loss_rejected(self, scorer):
        """Loss much greater than fee tolerance should be rejected."""
        assert scorer.gate(_features(cycle={"cycle_length": 3, "net_loss_ratio": 0.50})) is False

    def test_valid_cycle_passes(self, scorer):
        assert scorer.gate(_features(cycle={"cycle_length": 3, "net_loss_ratio": 0.04})) is True


class TestScore:
    def test_high_similarity_scores_well(self, scorer):
        cycle = {
            "cycle_length": 3,
            "addresses": ["a", "b", "c"],
            "amount_similarity": 0.95,
            "net_loss_ratio": 0.03,
            "recurrence_count": 4,
            "recipient_entropy": 0.40,
            "round_amount_flag": True,
            "temporal_concentration": 0.70,
            "mean_inter_hop_delta_slots": 3.0,
            "origin_cluster": "cluster01",
        }
        result = scorer.score(_features(cycle=cycle))
        assert result.score > 30
        assert result.sub_scores["amount_similarity"] > 0.5

    def test_low_entropy_boosts_score(self, scorer):
        """Low recipient entropy (same addresses) should increase entropy_inv sub-score."""
        high_entropy = {
            "cycle_length": 4, "amount_similarity": 0.80,
            "net_loss_ratio": 0.04, "recurrence_count": 1,
            "recipient_entropy": 0.90,  # high = normal
            "round_amount_flag": False, "temporal_concentration": 0.2,
            "mean_inter_hop_delta_slots": 20, "origin_cluster": "c",
        }
        low_entropy = dict(high_entropy, recipient_entropy=0.15)  # low = suspicious
        r_high = scorer.score(_features(cycle=high_entropy))
        r_low = scorer.score(_features(cycle=low_entropy))
        assert r_low.sub_scores["recipient_entropy_inv"] > r_high.sub_scores["recipient_entropy_inv"]

    def test_no_cycle_returns_zero(self, scorer):
        result = scorer.score(_features())
        assert result.score == 0.0

    def test_sub_scores_keys(self, scorer):
        cycle = {
            "cycle_length": 2, "amount_similarity": 0.80,
            "net_loss_ratio": 0.02, "recurrence_count": 0,
            "recipient_entropy": 0.70, "round_amount_flag": False,
            "temporal_concentration": 0.0, "mean_inter_hop_delta_slots": 50,
            "origin_cluster": "x",
        }
        result = scorer.score(_features(cycle=cycle))
        for key in ("amount_similarity", "cycle_recurrence", "recipient_entropy_inv",
                     "auxiliary", "speed"):
            assert key in result.sub_scores
