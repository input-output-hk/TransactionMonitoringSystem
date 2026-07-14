"""Unit tests for the Front-Running scorer (Class 5)."""

import pytest

from app.analysis.scorers.front_running import FrontRunningScorer


@pytest.fixture
def scorer():
    return FrontRunningScorer()


def _features(collision=None, fee=200_000):
    return {
        "tx_hash": "fr01",
        "network": "preprod",
        "fee": fee,
        "raw_data": {"timeToLive": 500},
        "collision": collision,
    }


class TestGate:
    def test_no_collision_data(self, scorer):
        assert scorer.gate(_features()) is False

    def test_zero_shared_inputs(self, scorer):
        assert scorer.gate(_features(collision={"shared_inputs": 0})) is False

    def test_one_shared_input_passes(self, scorer):
        assert scorer.gate(_features(collision={"shared_inputs": 1})) is True


class TestScore:
    def test_confirmed_collision_high_score(self, scorer):
        collision = {
            "counterpart_tx": "other01",
            "shared_inputs": 2,
            "delta_ms": 150.0,
            "outcome": "TX1_FAILS_UTXO_SPENT",
            "counterpart_fee": 210_000,
            "counterpart_ttl": 490,
            "shares_change_address": True,
            "attacker_win_count": 5,
        }
        result = scorer.score(_features(collision=collision))
        assert result.score > 40
        assert result.sub_scores["collision_outcome"] == 1.0
        assert "confirmed_utxo_collision" in result.reasons

    def test_small_delta_boosts_score(self, scorer):
        """200ms delta should produce high mempool_delta_inv sub-score."""
        fast = {
            "shared_inputs": 1,
            "delta_ms": 200.0,
            "outcome": "TX1_FAILS_UTXO_SPENT",
            "attacker_win_count": 1,
        }
        slow = {
            "shared_inputs": 1,
            "delta_ms": 5000.0,
            "outcome": "TX1_FAILS_UTXO_SPENT",
            "attacker_win_count": 1,
        }
        r_fast = scorer.score(_features(collision=fast))
        r_slow = scorer.score(_features(collision=slow))
        assert r_fast.sub_scores["mempool_delta_inv"] > r_slow.sub_scores["mempool_delta_inv"]

    def test_recurrence_cap(self, scorer):
        """Low win_count + high raw score should be capped at 79."""
        collision = {
            "shared_inputs": 3,
            "delta_ms": 100.0,
            "outcome": "TX1_FAILS_UTXO_SPENT",
            "counterpart_fee": 200_000,
            "counterpart_ttl": 500,
            "shares_change_address": True,
            "attacker_win_count": 1,  # below minimum 3
        }
        result = scorer.score(_features(collision=collision))
        # Even if raw would push above 80, cap at 79
        assert result.score <= 79.0

    def test_no_collision_returns_zero(self, scorer):
        result = scorer.score(_features())
        assert result.score == 0.0
