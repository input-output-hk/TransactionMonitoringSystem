"""Unit tests for the Large Datum scorer (Class 3)."""

import hashlib
import os

import pytest

from app.analysis.normalise import (
    BAND_CRITICAL_THRESHOLD,
    BAND_HIGH_THRESHOLD,
    BAND_MODERATE_MAX,
)
from app.analysis.scorers import large_datum as ldm
from app.analysis.scorers.large_datum import (
    _MIN_DATUM_BYTES,
    _SIZE_BACKSTOP,
    _W,
    LargeDatumScorer,
)
from tests.analysis.scorers.conftest import features_for_outputs as _features


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
        feats = _features([_out(SCRIPT, datum=_high_entropy_datum(13000))])
        assert scorer.gate(feats) is True
        # Asserted at the SCORE level too: gate() alone cannot see a band
        # change, so a gate-only test would stay green through a downgrade of
        # exactly the evasion this case exists to cover.
        assert scorer.score(feats).score >= BAND_CRITICAL_THRESHOLD

    def test_high_entropy_single_leaf_padding_gates(self, scorer):
        # The exact entropy-gate evasion: a single CBOR ByteArray of ~9KB random
        # (high-entropy) padding, below the size backstop. The entropy branch
        # misses it, but leaf-concentration (~1.0) catches it structurally.
        cbor2 = pytest.importorskip("cbor2")
        datum = cbor2.dumps(os.urandom(9000)).hex()  # one giant leaf
        feats = _features([_out(SCRIPT, datum=datum)])
        # Below the backstop, so concentration is the trigger.
        assert len(datum) // 2 < _SIZE_BACKSTOP
        assert scorer.gate(feats) is True

    def test_high_entropy_structured_datum_not_gated(self, scorer):
        # A registry-like large datum: 250 distinct 32-byte random leaves. High
        # entropy AND low concentration (~0.004) -> not bloat. Confirms the
        # concentration trigger does not false-positive on rich nested state.
        cbor2 = pytest.importorskip("cbor2")
        datum = cbor2.dumps([os.urandom(32) for _ in range(250)]).hex()
        feats = _features([_out(SCRIPT, datum=datum)])
        assert _MIN_DATUM_BYTES <= len(datum) // 2 < _SIZE_BACKSTOP  # above floor, below backstop
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
        medium = _out(SCRIPT, lovelace=2_000_000, datum="aa" * 9000)  # 9000 bytes
        large = _out(SCRIPT, lovelace=2_000_000, datum="ff" * 13000)  # 13000 bytes
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
        assert result.score >= BAND_HIGH_THRESHOLD

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


def _uncapped_score(result):
    """The score the weights alone would have produced, from the sub-scores.

    Lets a cap assertion prove the cap is load-bearing rather than incidental,
    and a no-cap assertion prove the fixture really was in alerting territory.
    Mirrors the weighting in ``LargeDatumScorer._score_utxo``.
    """
    return 100.0 * (
        float(_W["datum_bytes"]) * result.sub_scores["datum_bytes"]
        + float(_W["datum_ratio"]) * result.sub_scores["datum_ratio"]
        + float(_W["value_cbor_inv"]) * result.sub_scores["value_cbor_bytes_inverted"]
        + float(_W["recurrence"]) * result.sub_scores["sender_recurrence"]
    )


class TestBackstopOnlyRecall:
    """A finding the size backstop admits ALONE keeps its full alerting band.

    Clearing the two content branches is not evidence of legitimacy: the
    attacker writes the datum, so every "benign" shape is reproducible at a
    victim script, and some deliveries are not measurable at all. Each test
    here is a shape that a content-keyed downgrade would silence, pinned at the
    SCORE level (a ``gate()``-only assertion cannot see a band change).
    """

    def test_high_entropy_multi_leaf_padding_stays_critical(self, scorer):
        # 400 random 32-byte CBOR leaves: 13 KB of pure padding that reads as
        # legitimate registry state on BOTH content axes (entropy ~7.86 > 4.0,
        # concentration ~0.002 < 0.5). Chunked leaves are also the natural
        # on-chain encoding, since Plutus Data bounds each ByteString at 64
        # bytes, so this is the cheap shape, not an exotic one.
        cbor2 = pytest.importorskip("cbor2")
        datum = cbor2.dumps([os.urandom(32) for _ in range(400)]).hex()
        assert len(datum) // 2 >= _SIZE_BACKSTOP
        out = _out(SCRIPT, lovelace=55_000_000, datum=datum)
        result = scorer.score(_features([out]))
        assert result.evidence["bloat_trigger"] == "size_backstop"
        assert "size_backstop_only" in result.reasons
        assert result.score >= BAND_CRITICAL_THRESHOLD

    def test_hash_delivered_padding_stays_critical(self, scorer):
        # The same padding attack delivered as datumHash + witness preimage.
        # The byte gates size it from the preimage, so the content
        # discriminators must read the same bytes: assessing only
        # output["datum"] made entropy report its 8.0 "unmeasurable" default and
        # concentration 0.0, which is indistinguishable from a benign verdict.
        # Choosing hash delivery must not change the band.
        datum = _low_entropy_datum(13000)
        digest = hashlib.blake2b(bytes.fromhex(datum), digest_size=32).hexdigest()
        feats = _features([_out(SCRIPT, lovelace=2_000_000, datum_hash=digest)])
        feats["raw_data"]["datums"] = {digest: datum}
        result = scorer.score(feats)
        assert result.evidence["datum_type"] == "hash"
        assert result.evidence["bloat_trigger"] == "content"
        assert result.score >= BAND_CRITICAL_THRESHOLD

    def test_hash_delivered_padding_below_the_backstop_is_found(self, scorer):
        # Coverage this scorer did not previously have, rather than a
        # restoration: a hash-delivered datum between min_datum_bytes and the
        # size backstop used to fall in a hole. It is too small for the
        # backstop, and the content gate read output["datum"], which a hash
        # output does not have, so nothing engaged at all. Reading the preimage
        # closes the hole, and the score must match the same bytes delivered
        # inline: how a datum is delivered is not a property of the attack.
        nbytes = (_MIN_DATUM_BYTES + _SIZE_BACKSTOP) // 2
        datum = _low_entropy_datum(nbytes)
        assert _MIN_DATUM_BYTES <= nbytes < _SIZE_BACKSTOP
        digest = hashlib.blake2b(bytes.fromhex(datum), digest_size=32).hexdigest()
        feats = _features([_out(SCRIPT, lovelace=2_000_000, datum_hash=digest)])
        feats["raw_data"]["datums"] = {digest: datum}
        assert scorer.gate(feats) is True
        by_hash = scorer.score(feats)
        assert by_hash.evidence["bloat_trigger"] == "content"
        assert by_hash.score >= BAND_HIGH_THRESHOLD

        inline = scorer.score(_features([_out(SCRIPT, lovelace=2_000_000, datum=datum)]))
        assert by_hash.score == inline.score

    def test_unwalkable_cbor_stays_critical_and_is_marked(self, scorer):
        # Nesting past cbor2's 400-container limit makes leaf concentration
        # unmeasurable (it returns 0.0, its "not concentrated" default). An
        # attacker controls the encoding, so an unreadable datum is recorded as
        # unread, never treated as read-and-benign.
        cbor2 = pytest.importorskip("cbor2")
        datum = (b"\x81" * 500 + cbor2.dumps(os.urandom(13000))).hex()
        out = _out(SCRIPT, lovelace=2_000_000, datum=datum)
        result = scorer.score(_features([out]))
        assert result.evidence["bloat_trigger"] == "size_backstop_unassessed"
        assert "size_backstop_content_unreadable" in result.reasons
        assert result.score >= BAND_CRITICAL_THRESHOLD

    def test_low_entropy_oversized_datum_stays_critical(self, scorer):
        # Oversized AND low-entropy is bloat twice over: content-triggered.
        out = _out(SCRIPT, lovelace=2_000_000, datum=_low_entropy_datum(13000))
        result = scorer.score(_features([out]))
        assert result.evidence["bloat_trigger"] == "content"
        assert result.score >= BAND_CRITICAL_THRESHOLD

    def test_single_leaf_oversized_padding_stays_critical(self, scorer):
        # The entropy-gate evasion: one giant random ByteArray past the
        # backstop, caught structurally by leaf concentration.
        cbor2 = pytest.importorskip("cbor2")
        datum = cbor2.dumps(os.urandom(13000)).hex()
        out = _out(SCRIPT, lovelace=2_000_000, datum=datum)
        result = scorer.score(_features([out]))
        assert result.evidence["bloat_trigger"] == "content"
        assert result.score >= BAND_CRITICAL_THRESHOLD


class TestLargeStateAllowlistCap:
    """Only an allowlisted script's backstop-only finding is capped.

    The suppression exists for one observed shape: a state-machine contract
    whose normal state IS a near-backstop datum, re-spending that UTxO on every
    transition (mainnet 2026-07-26: one script, 106 spends in 67 minutes at a
    fixed ~12.5 KB inline datum, every spend Critical). Re-spending falsifies
    the backstop's "this UTxO can no longer be spent" premise, and with no
    per-script baseline yet and recurrence stubbed to 0 the score cannot tell
    the difference. Identity is what carries the exemption: only the contract's
    operator controls its address.
    """

    @pytest.fixture
    def allowlisted(self, monkeypatch):
        monkeypatch.setattr(ldm, "_LARGE_STATE_ALLOWLIST", {"preprod": (SCRIPT,)})

    def _structured_oversized(self):
        cbor2 = pytest.importorskip("cbor2")
        return cbor2.dumps([os.urandom(32) for _ in range(400)]).hex()

    def test_allowlisted_backstop_only_finding_is_capped(self, scorer, allowlisted):
        out = _out(SCRIPT, lovelace=55_000_000, datum=self._structured_oversized())
        result = scorer.score(_features([out]))
        assert "large_state_allowlisted" in result.reasons
        assert result.score <= BAND_MODERATE_MAX
        # The cap must be load-bearing: without it this was an alerting
        # Critical. Guards against a weight change making the test vacuous.
        assert _uncapped_score(result) >= BAND_CRITICAL_THRESHOLD

    def test_capped_finding_still_records_full_evidence(self, scorer, allowlisted):
        # Capping the band must not suppress the finding: sub-scores and
        # evidence stay intact so an analyst can review and promote it.
        out = _out(SCRIPT, lovelace=55_000_000, datum=self._structured_oversized())
        result = scorer.score(_features([out]))
        assert result.score > 0
        assert result.sub_scores["datum_bytes"] > 0
        assert result.evidence["datum_bytes_raw"] >= _SIZE_BACKSTOP
        assert result.evidence["datum_type"] == "inline"

    def test_allowlist_does_not_cap_a_content_triggered_finding(self, scorer, allowlisted):
        # An allowlisted contract that starts emitting padding still pages.
        out = _out(SCRIPT, lovelace=2_000_000, datum=_low_entropy_datum(13000))
        result = scorer.score(_features([out]))
        assert "large_state_allowlisted" not in result.reasons
        assert result.score >= BAND_CRITICAL_THRESHOLD

    def test_allowlist_does_not_cap_unreadable_content(self, scorer, allowlisted):
        # Nor does it cap a datum whose content could not be read: the
        # exemption is for a contract observed serving MEASURED benign state.
        cbor2 = pytest.importorskip("cbor2")
        datum = (b"\x81" * 500 + cbor2.dumps(os.urandom(13000))).hex()
        out = _out(SCRIPT, lovelace=2_000_000, datum=datum)
        result = scorer.score(_features([out]))
        assert "large_state_allowlisted" not in result.reasons
        assert result.score >= BAND_CRITICAL_THRESHOLD

    def test_non_allowlisted_script_is_not_capped(self, scorer, monkeypatch):
        monkeypatch.setattr(ldm, "_LARGE_STATE_ALLOWLIST", {"preprod": ("addr_test1wOTHER",)})
        out = _out(SCRIPT, lovelace=55_000_000, datum=self._structured_oversized())
        result = scorer.score(_features([out]))
        assert "large_state_allowlisted" not in result.reasons
        assert result.score >= BAND_CRITICAL_THRESHOLD

    def test_shipped_allowlist_is_empty_on_every_network(self):
        # The recall-safe default: nothing is suppressed until an operator adds
        # a script they have verified re-spends its own large-datum UTxO.
        for network in ("mainnet", "preprod", "preview"):
            assert ldm._LARGE_STATE_ALLOWLIST.get(network, ()) == ()

    def test_allowlist_is_network_scoped(self, scorer, monkeypatch):
        # A preprod entry must never suppress a mainnet finding.
        monkeypatch.setattr(ldm, "_LARGE_STATE_ALLOWLIST", {"preprod": (SCRIPT,)})
        feats = _features([_out(SCRIPT, lovelace=55_000_000, datum=self._structured_oversized())])
        feats["network"] = "mainnet"
        result = scorer.score(feats)
        assert "large_state_allowlisted" not in result.reasons
        assert result.score >= BAND_CRITICAL_THRESHOLD


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
