"""Unit tests for the Large Datum scorer (Class 3)."""

import pytest
from app.analysis.scorers.large_datum import LargeDatumScorer


@pytest.fixture
def scorer():
    return LargeDatumScorer()


SCRIPT = "addr_test1wz5fxvalex"
WALLET = "addr_test1qz5fxvalex"


def _out(address, lovelace=2_000_000, datum=None, datum_hash=None):
    o = {"address": address, "value": {"lovelace": lovelace}}
    if datum is not None:
        o["datum"] = datum
    if datum_hash is not None:
        o["datumHash"] = datum_hash
    return o


def _features(outputs):
    return {"tx_hash": "ld01", "network": "preprod", "raw_data": {"outputs": outputs}}


class TestGate:
    def test_no_datum_rejected(self, scorer):
        assert scorer.gate(_features([_out(SCRIPT)])) is False

    def test_wallet_rejected(self, scorer):
        assert scorer.gate(_features([_out(WALLET, datum="aa" * 100)])) is False

    def test_datum_hash_only_rejected(self, scorer):
        """datumHash without inline datum has 0 bytes, gate should fail."""
        assert scorer.gate(_features([_out(SCRIPT, datum_hash="abc123")])) is False

    def test_inline_datum_above_floor_passes(self, scorer):
        # 7000-byte datum, above the 6000-byte gate floor.
        assert scorer.gate(_features([_out(SCRIPT, datum="aa" * 7000)])) is True

    def test_inline_datum_below_floor_rejected(self, scorer):
        """A normal-size (200-byte) datum is below the bloat-detection floor."""
        assert scorer.gate(_features([_out(SCRIPT, datum="aa" * 200)])) is False


class TestScore:
    def test_large_datum_high_ratio(self, scorer):
        """A 9000-byte datum with minimal value should score high on datum_ratio."""
        out = _out(SCRIPT, lovelace=2_000_000, datum="ff" * 9000)
        result = scorer.score(_features([out]))
        assert result.sub_scores["datum_ratio"] > 0.3
        assert result.score > 20

    def test_larger_datum_scores_higher(self, scorer):
        """Above the gate floor, a larger datum should score higher."""
        medium = _out(SCRIPT, lovelace=2_000_000, datum="aa" * 7000)   # 7000 bytes
        large = _out(SCRIPT, lovelace=2_000_000, datum="ff" * 13000)   # 13000 bytes
        r_medium = scorer.score(_features([medium]))
        r_large = scorer.score(_features([large]))
        assert r_medium.score < r_large.score

    def test_sub_scores_keys(self, scorer):
        out = _out(SCRIPT, datum="bb" * 7000)  # 7000 bytes, above gate
        result = scorer.score(_features([out]))
        for key in ("datum_bytes", "datum_ratio", "value_cbor_bytes_inverted"):
            assert key in result.sub_scores


# Two bech32-decodable preprod script addresses with distinct payment
# credentials. Using real decode-able addresses lets _payment_credential
# group correctly across stake-cred variants of the same script in tests
# that exercise the per-script aggregation path.
_SCRIPT_A = (
    "addr_test1zq3kpwwmyqpppm49huqghuttgda85mkncdps99jne0ad6xed"
    "anvqr0pyy3ne06uvxkaalx8ds4x55z9gq6znqp5p06xqhwh4ht"
)
_SCRIPT_B = (
    "addr_test1zpsqdy4efletcs8d6pgzjrxmjq6gg82dr5fyvepn9yv09l"
    "d285x8fy9ezxxyczxq0rfc3m5rfl6yj6ex3ecxx70xngnsf52z3z"
)


class TestAggregateEngagement:
    """Multi-output datum-bloat observability path.

    When an attacker splits the bloat payload across N script outputs at
    the SAME contract, each below the per-output gate, the scorer
    engages to surface `max_script_datum_bytes` in sub_scores. Per-output
    scoring does not fire (no DoS alert), and `max_class` does not become
    `large_datum` (score returned as -1).
    """

    def test_aggregate_at_same_script_engages_observability(self, scorer):
        # 4 outputs x 3500 bytes at the same script. Aggregate = 14000B,
        # crosses the 12000B engagement threshold. No single output
        # crosses the 6000B per-output predicate.
        outs = [_out(_SCRIPT_A, datum="aa" * 3500) for _ in range(4)]
        feats = _features(outs)
        assert scorer.gate(feats) is True
        result = scorer.score(feats)
        # Score is negative so the engine does not classify the tx as
        # large_datum (the default -1 sentinel means "no finding").
        assert result.score == -1.0
        assert result.reasons == []
        assert result.sub_scores == {"max_script_datum_bytes": 14000.0}

    def test_aggregate_across_distinct_scripts_does_not_engage(self, scorer):
        # 4 outputs of 3500 bytes split 2/2 across two unrelated scripts.
        # Tx-wide sum = 14000B but no single script aggregates to >= 12000B,
        # so the gate must NOT engage. This is the regression for finding
        # #1 of the review: cross-script aggregation was incorrectly
        # treated as same-script bloat.
        outs = [
            _out(_SCRIPT_A, datum="aa" * 3500),
            _out(_SCRIPT_A, datum="aa" * 3500),
            _out(_SCRIPT_B, datum="bb" * 3500),
            _out(_SCRIPT_B, datum="bb" * 3500),
        ]
        feats = _features(outs)
        # Each per-script aggregate is 7000B < 12000B.
        assert scorer.gate(feats) is False

    def test_per_output_predicate_unchanged_by_aggregate_path(self, scorer):
        # A 9000B single-output datum still fires the per-output predicate
        # and produces a real score, with the new sub-score recording the
        # same-script aggregate (here equal to the single datum size).
        out = _out(_SCRIPT_A, lovelace=2_000_000, datum="ff" * 9000)
        result = scorer.score(_features([out]))
        assert result.score > 0
        assert "large_datum_bytes" in result.reasons
        assert result.sub_scores.get("max_script_datum_bytes") == 9000.0
