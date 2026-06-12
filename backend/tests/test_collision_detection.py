"""Unit tests for the front-running scorer with mock collision data."""

import pytest
from app.analysis.scorers.front_running import FrontRunningScorer


@pytest.fixture
def scorer():
    return FrontRunningScorer()


def _features(collision=None, raw_data=None):
    return {
        "tx_hash": "fr01",
        "network": "preprod",
        "fee": 200_000,
        "raw_data": raw_data if raw_data is not None else {"timeToLive": 1000},
        "collision": collision,
    }


class TestFrontRunningGate:
    def test_no_collision_fails(self, scorer):
        assert scorer.gate(_features()) is False

    def test_collision_with_shared_inputs(self, scorer):
        c = {"shared_inputs": 2, "delta_ms": 500, "outcome": "BOTH_PENDING"}
        assert scorer.gate(_features(collision=c)) is True

    def test_collision_zero_shared_fails(self, scorer):
        c = {"shared_inputs": 0}
        assert scorer.gate(_features(collision=c)) is False


class TestFrontRunningScore:
    def test_confirmed_collision_scores_high(self, scorer):
        c = {
            "counterpart_tx": "tx_other",
            "shared_inputs": 3,
            "delta_ms": 150.0,
            "outcome": "TX1_FAILS_UTXO_SPENT",
            "counterpart_fee": 250_000,
            "counterpart_ttl": 1050,
            "shares_change_address": True,
            "attacker_win_count": 5,
        }
        result = scorer.score(_features(collision=c))
        assert result.score > 30
        assert "collision_outcome" in result.sub_scores
        assert result.sub_scores["outcome"] == "TX1_FAILS_UTXO_SPENT"

    def test_ambiguous_outcome_lower(self, scorer):
        c = {
            "counterpart_tx": "tx_other",
            "shared_inputs": 1,
            "delta_ms": 5000.0,
            "outcome": "BOTH_PENDING",
            "counterpart_fee": 200_000,
            "counterpart_ttl": 1000,
            "shares_change_address": False,
            "attacker_win_count": 0,
        }
        result = scorer.score(_features(collision=c))
        assert result.score < 50

    def test_no_collision_returns_zero(self, scorer):
        result = scorer.score(_features())
        assert result.score == 0.0

    def test_v6_validity_interval_feeds_ttl_similarity(self, scorer):
        """Regression: TTL was read from the v5 ``timeToLive`` field only, so
        on an Ogmios v6 node every tx read ttl=0 and ttl_sim pinned at 1.0.
        With a v6 ``validityInterval`` far from the counterpart TTL, the
        structure axis must score LOWER than a same-TTL pair."""
        base = {
            "counterpart_tx": "tx_other",
            "shared_inputs": 3,
            "delta_ms": 150.0,
            "outcome": "TX1_FAILS_UTXO_SPENT",
            "counterpart_fee": 200_000,
            "counterpart_ttl": 1000,
            "shares_change_address": False,
            "attacker_win_count": 5,
        }
        far = scorer.score(_features(
            collision=dict(base),
            raw_data={"validityInterval": {"invalidAfter": 50_000}},
        ))
        near = scorer.score(_features(
            collision=dict(base),
            raw_data={"validityInterval": {"invalidAfter": 1000}},
        ))
        assert near.score > far.score

    def test_recurrence_cap(self, scorer):
        """Low recurrence should cap score below Critical."""
        c = {
            "counterpart_tx": "tx_other",
            "shared_inputs": 5,
            "delta_ms": 50.0,
            "outcome": "TX1_FAILS_UTXO_SPENT",
            "counterpart_fee": 200_000,
            "counterpart_ttl": 1000,
            "shares_change_address": True,
            "attacker_win_count": 1,  # below threshold
        }
        result = scorer.score(_features(collision=c))
        assert result.score <= 79.0
