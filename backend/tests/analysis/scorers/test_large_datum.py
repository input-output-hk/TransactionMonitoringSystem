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
