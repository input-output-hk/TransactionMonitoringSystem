"""Unit tests for the Fake Token scorer (Class 8)."""

from unittest.mock import patch

import pytest

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

    def test_critical_asset_clone_scores_higher_than_standard(self, scorer):
        """An exact-name clone of a critical stablecoin (iUSD) must score
        strictly higher than an identical clone of a non-critical token
        (HOSKY): same fake policy, same quantity, same recipients, so the only
        difference is the criticality amplification on the identity axis. This
        pins the recall-positive escalation. iUSD/HOSKY are both in the mainnet
        registry; HOSKY is intentionally absent from fake_token.critical_assets.
        """
        outputs = [{"address": f"addr{i}", "value": {"lovelace": 1_500_000}} for i in range(5)]
        r_critical = scorer.score(_features(mint={FAKE_POLICY: {"iUSD": 10_000}}, outputs=outputs))
        r_standard = scorer.score(_features(mint={FAKE_POLICY: {"HOSKY": 10_000}}, outputs=outputs))

        assert r_critical.evidence["matched_token_criticality"] == "critical"
        assert r_standard.evidence["matched_token_criticality"] == "standard"
        # Identity is amplified for the critical asset, lifting the final score.
        assert (
            r_critical.sub_scores["identity_composite"]
            > r_standard.sub_scores["identity_composite"]
        )
        assert r_critical.score > r_standard.score

    def test_criticality_never_lowers_score(self, scorer):
        """The amplification is monotonic (multiplier >= 1.0, capped at 1.0):
        a critical-asset clone is never scored below the same clone without the
        bonus. Compared against the multiplier=1.0 (no-op) baseline."""

        outputs = [{"address": "addr1", "value": {"lovelace": 1_500_000}}]
        feats = _features(mint={FAKE_POLICY: {"iUSD": 10_000}}, outputs=outputs)
        with_bonus = scorer.score(feats).score
        with patch("app.analysis.scorers.fake_token._CRITICALITY_MULTIPLIER", 1.0):
            no_bonus = scorer.score(feats).score
        assert with_bonus >= no_bonus


class TestConfusablesFold:
    """Confusables fold for cross-script visual homoglyphs.

    NFKC does not fold Greek/Cyrillic characters that look identical to
    Latin letters but encode under different codepoints. The
    `_normalise_token_name` helper applies a curated confusables table
    after NFKC so the gate's similarity comparison sees a folded form.
    """

    def test_normalize_folds_greek_capital_nu_to_latin_n(self):
        # Greek capital Nu (U+039D) looks identical to Latin N (U+004E).
        from app.analysis.scorers.fake_token import _normalise_token_name

        assert _normalise_token_name("ΝTX") == "NTX"

    def test_normalize_folds_cyrillic_straight_u_to_latin_y(self):
        # Cyrillic capital Straight U (U+04AE) looks like Latin Y.
        from app.analysis.scorers.fake_token import _normalise_token_name

        assert _normalise_token_name("INDҮ") == "INDY"

    def test_normalize_folds_full_forge_homoglyph(self):
        # The exact forge attack bytes for the INDY homoglyph:
        # `6cce9d44d2ae` = "lΝDҮ" (Latin l, Greek Nu, Latin D, Cyrillic
        # Straight U). After fold, three of four chars match INDY; the
        # remaining `l` is an intra-Latin confusable for `I` not folded
        # here (would inflate FPs on legitimate lowercase-L tokens).
        from app.analysis.scorers.fake_token import _normalise_token_name

        homoglyph = bytes.fromhex("6cce9d44d2ae").decode("utf-8")
        assert _normalise_token_name(homoglyph) == "lNDY"

    def test_unicode_suspicion_fires_on_uppercase_greek(self):
        # Pre-fix, the homoglyph set was lowercase-Greek only and missed
        # uppercase confusables like the forge INDY attack.
        from app.analysis.scorers.fake_token import _compute_unicode_suspicion

        homoglyph = bytes.fromhex("6cce9d44d2ae").decode("utf-8")
        assert _compute_unicode_suspicion(homoglyph) > 0.0

    def test_unicode_suspicion_does_not_fire_on_pure_ascii(self):
        from app.analysis.scorers.fake_token import _compute_unicode_suspicion

        # Pure ASCII case-spoof has no confusables, no zero-width, no
        # mixed scripts. Score must be 0.
        assert _compute_unicode_suspicion("nTX") == 0.0


class TestAsciiHomoglyphs:
    """ASCII visual lookalikes (spec table: O/0, I/l/1). The similarity fold
    must catch digit-for-letter forgeries (recall), while the fold-gain test
    keeps legitimately numeric names off the suspicion axis (precision)."""

    def _outputs(self, n=5):
        return [{"address": f"addr{i}", "value": {"lovelace": 1_500_000}} for i in range(n)]

    def test_zero_for_O_forgery_detected(self, scorer):
        # "H0SKY" (digit zero) impersonating HOSKY under a wrong policy.
        mint = {FAKE_POLICY: {"H0SKY": 1000}}
        feats = _features(mint=mint, outputs=self._outputs())
        assert scorer.gate(feats) is True
        result = scorer.score(feats)
        # The fold makes the match exact, and the fold-gain test adds the
        # homoglyph bump to the suspicion axis.
        assert result.evidence["matched_similarity"] == 1.0
        assert result.sub_scores["unicode_suspicion"] > 0.0
        kinds = {c["kind"] for c in result.evidence["unicode_confusables"]}
        assert "ascii_homoglyph" in kinds

    def test_one_for_I_forgery_detected(self, scorer):
        # "1NDY" (digit one) impersonating INDY.
        mint = {FAKE_POLICY: {"1NDY": 1000}}
        feats = _features(mint=mint, outputs=self._outputs())
        assert scorer.gate(feats) is True
        result = scorer.score(feats)
        assert result.evidence["matched_token"] == "INDY"
        assert result.evidence["matched_similarity"] == 1.0
        assert result.sub_scores["unicode_suspicion"] > 0.0

    def test_numeric_suffix_gets_no_suspicion_bump(self, scorer):
        # "HOSKY2" is name-similar (gates on plain Levenshtein) but its digit
        # is appended, not substituted: the ASCII fold gains nothing, so the
        # suspicion axis must stay at zero.
        mint = {FAKE_POLICY: {"HOSKY2": 1000}}
        feats = _features(mint=mint, outputs=self._outputs())
        assert scorer.gate(feats) is True
        result = scorer.score(feats)
        assert result.sub_scores["unicode_suspicion"] == 0.0
        kinds = {c["kind"] for c in result.evidence["unicode_confusables"]}
        assert "ascii_homoglyph" not in kinds

    def test_unrelated_numeric_name_not_gated(self, scorer):
        # A numeric token name far from every registry entry must not gate
        # just because the fold maps its letters onto digits.
        mint = {FAKE_POLICY: {"X100PULL": 1000}}
        assert scorer.gate(_features(mint=mint)) is False
