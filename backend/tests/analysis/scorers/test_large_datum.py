"""Unit tests for the Large Datum scorer (Class 3)."""

import os

import pytest
from app.analysis.normalise import BAND_CRITICAL_THRESHOLD
from app.analysis.scorers.large_datum import LargeDatumScorer, _MIN_DATUM_BYTES


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


def _low_entropy_datum(nbytes):
    """Padding-bloat datum: a single repeated byte (Shannon entropy ~0)."""
    return "aa" * nbytes


def _high_entropy_datum(nbytes):
    """Structured/legit datum: bytes cycling 0x00-0xFF (entropy ~8 bits/byte)."""
    return bytes((i % 256) for i in range(nbytes)).hex()


def _features(outputs):
    return {"tx_hash": "ld01", "network": "preprod", "raw_data": {"outputs": outputs}}


class TestGate:
    def test_no_datum_rejected(self, scorer):
        assert scorer.gate(_features([_out(SCRIPT)])) is False

    def test_wallet_rejected(self, scorer):
        assert scorer.gate(_features([_out(WALLET, datum=_low_entropy_datum(9000))])) is False

    def test_datum_hash_only_engages_observability(self, scorer):
        """datumHash without inline datum has 0 bytes (unsizable without an
        indexer): the gate engages so the shape is recorded, but score()
        returns a no-finding (-1) and never alerts."""
        feats = _features([_out(SCRIPT, datum_hash="abc123")])
        assert scorer.gate(feats) is True
        result = scorer.score(feats)
        assert result.score == -1.0
        assert result.sub_scores["datum_hash_only_count"] == 1.0
        assert result.evidence["datum_hash_only_addresses"] == [SCRIPT]
        assert result.reasons == []

    def test_datum_hash_only_at_wallet_still_rejected(self, scorer):
        """The observability path is script-gated like everything else."""
        assert scorer.gate(_features([_out(WALLET, datum_hash="abc123")])) is False

    def test_low_entropy_datum_above_floor_gates(self, scorer):
        # A large, low-entropy (repetitive padding) datum is a bloat candidate.
        assert _MIN_DATUM_BYTES == 6000
        assert scorer.gate(_features([_out(SCRIPT, datum=_low_entropy_datum(9000))])) is True

    def test_low_entropy_datum_below_floor_rejected(self, scorer):
        """A small datum is below the size floor even if low-entropy."""
        assert scorer.gate(_features([_out(SCRIPT, datum=_low_entropy_datum(200))])) is False

    def test_high_entropy_mid_size_datum_rejected(self, scorer):
        # A large but high-entropy datum carries structured contract state, not
        # padding bloat. At 9 KB it is between min_datum_bytes and the absolute
        # size backstop, so it is NOT flagged: this is the documented residual
        # gap (a high-entropy datum sized within the legitimate range evades),
        # and it is what removes the 156 false positives.
        assert scorer.gate(_features([_out(SCRIPT, datum=_high_entropy_datum(9000))])) is False

    def test_high_entropy_extreme_bloat_gates(self, scorer):
        # A high-entropy (random-padded) datum that approaches the tx-size limit
        # is flagged by the absolute size backstop REGARDLESS of entropy, since
        # a consuming tx could no longer fit. This is the defence against the
        # high-entropy evasion of the entropy gate.
        assert scorer.gate(_features([_out(SCRIPT, datum=_high_entropy_datum(13000))])) is True

    def test_high_entropy_single_leaf_padding_gates(self, scorer):
        # The exact entropy-gate evasion: a single CBOR ByteArray of ~9KB random
        # (high-entropy) padding, below the size backstop. The entropy branch
        # misses it, but leaf-concentration (~1.0) catches it structurally.
        cbor2 = pytest.importorskip("cbor2")
        datum = cbor2.dumps(os.urandom(9000)).hex()  # one giant leaf
        feats = _features([_out(SCRIPT, datum=datum)])
        assert len(datum) // 2 < 12288  # below the backstop, so concentration is the trigger
        assert scorer.gate(feats) is True

    def test_high_entropy_structured_datum_not_gated(self, scorer):
        # A registry-like large datum: 250 distinct 32-byte random leaves. High
        # entropy AND low concentration (~0.004) -> not bloat. Confirms the
        # concentration trigger does not false-positive on rich nested state.
        cbor2 = pytest.importorskip("cbor2")
        datum = cbor2.dumps([os.urandom(32) for _ in range(250)]).hex()
        feats = _features([_out(SCRIPT, datum=datum)])
        assert 6000 <= len(datum) // 2 < 12288  # above floor, below backstop
        assert scorer.gate(feats) is False

    def test_ctf04_sized_low_entropy_datum_gates(self, scorer):
        # Recall anchor: CTF-04's tipjar bloat was a ~7.3 KB datum of repeated
        # 0x41 padding (entropy ~0.3). It must gate despite overlapping the
        # benign ~6.9 KB contract in size.
        assert scorer.gate(_features([_out(SCRIPT, datum=_low_entropy_datum(7258))])) is True


class TestScore:
    def test_large_datum_high_ratio(self, scorer):
        """A 9000-byte datum with minimal value should score high on datum_ratio."""
        out = _out(SCRIPT, lovelace=2_000_000, datum="ff" * 9000)
        result = scorer.score(_features([out]))
        assert result.sub_scores["datum_ratio"] > 0.3
        assert result.score > 20

    def test_larger_datum_scores_higher(self, scorer):
        """Above the gate floor, a larger datum should score higher."""
        medium = _out(SCRIPT, lovelace=2_000_000, datum="aa" * 9000)    # 9000 bytes
        large = _out(SCRIPT, lovelace=2_000_000, datum="ff" * 13000)    # 13000 bytes
        r_medium = scorer.score(_features([medium]))
        r_large = scorer.score(_features([large]))
        assert r_medium.score < r_large.score

    def test_just_above_floor_not_critical(self, scorer):
        # A datum just over the gate floor is not a Critical: with weight on
        # absolute datum_bytes (not the saturating datum_ratio), only datums
        # approaching the full tx budget reach Critical. This is the
        # regression guard for the 156 false Criticals at ~7 KB.
        out = _out(SCRIPT, lovelace=2_000_000, datum="aa" * 8300)  # 8300 bytes
        result = scorer.score(_features([out]))
        assert result.score < BAND_CRITICAL_THRESHOLD

    def test_genuine_bloat_reaches_critical(self, scorer):
        # A datum at the bootstrap p99 (14000 bytes), genuinely threatening the
        # 16384-byte tx budget, still saturates the datum_bytes axis and reaches
        # Critical. Recall for real datum-bloat is preserved.
        out = _out(SCRIPT, lovelace=2_000_000, datum="ff" * 14000)  # 14000 bytes
        result = scorer.score(_features([out]))
        assert result.score >= BAND_CRITICAL_THRESHOLD

    def test_ctf04_sized_bloat_reaches_high(self, scorer):
        # Recall anchor: a ~7.3 KB low-entropy padding datum (CTF-04 shape) must
        # score High or above, not be suppressed. This is the regression the
        # byte-only gate caused and the entropy discriminator fixes.
        out = _out(SCRIPT, lovelace=2_000_000, datum=_low_entropy_datum(7258))
        result = scorer.score(_features([out]))
        assert result.score >= 60.0

    def test_high_entropy_large_datum_no_finding(self, scorer):
        # A large high-entropy (structured) datum is not bloat; scoring it
        # directly yields no finding (score -1), so it never alerts at any band.
        out = _out(SCRIPT, lovelace=2_000_000, datum=_high_entropy_datum(9000))
        result = scorer.score(_features([out]))
        assert result.score == -1.0

    def test_sub_scores_keys(self, scorer):
        out = _out(SCRIPT, datum="bb" * 9000)  # 9000 bytes, above gate
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
        assert result.sub_scores == {
            "max_script_datum_bytes": 14000.0,
            "datum_hash_only_count": 0.0,
        }

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
