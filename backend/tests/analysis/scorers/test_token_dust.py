"""Unit tests for the Token Dust scorer (Class 1)."""

import pytest
from app.analysis.normalise import BAND_HIGH_THRESHOLD
from app.analysis.scorers.token_dust import TokenDustScorer, _DOS_VALUE_CBOR_MIN
from app.analysis.features import _estimate_value_cbor_bytes

from tests.analysis.scorers.conftest import features_for_outputs as _features

# Recall pin for the symmetric many-policies DoS shape: it emits more
# value-map CBOR than the single-policy shape, so it must land deep in the
# High band, not just clear its lower edge. Pins the observed ~78 output
# with headroom for weight jitter while still failing on real suppression.
SYMMETRIC_DOS_MIN_SCORE = 75.0


@pytest.fixture
def scorer():
    return TokenDustScorer()


def _make_output(address, lovelace=2_000_000, policies=None):
    """Build a single output dict."""
    value = {"lovelace": lovelace}
    if policies:
        for pid, assets in policies.items():
            value[pid] = assets
    return {"address": address, "value": value}


SCRIPT_ADDR = "addr_test1wz5fxvalex"
WALLET_ADDR = "addr_test1qz5fxvalex"


class TestGate:
    def test_no_raw_data(self, scorer):
        assert scorer.gate({"raw_data": None}) is False

    def test_wallet_address_rejected(self, scorer):
        out = _make_output(WALLET_ADDR, policies={"policy1": {"tokenA": 1}})
        assert scorer.gate(_features([out])) is False

    def test_script_no_assets_rejected(self, scorer):
        out = _make_output(SCRIPT_ADDR)  # lovelace only
        assert scorer.gate(_features([out])) is False

    def test_script_with_single_asset_rejected(self, scorer):
        # A single-asset output cannot bloat the Value field's CBOR; the gate
        # requires >= min_token_count (default 2) live assets to engage.
        out = _make_output(SCRIPT_ADDR, policies={"policyA": {"tok1": 1}})
        assert scorer.gate(_features([out])) is False

    def test_small_benign_bundle_rejected(self, scorer):
        # 2-6 pair protocol multi-asset UTxOs (DEX pool state, lending offers)
        # are not plausible value-bloat DoS: too few pairs and tiny Value CBOR.
        # They must produce no finding at all (gate False), not a band-capped
        # Moderate alert. This is the dominant former false positive.
        out = _make_output(
            SCRIPT_ADDR,
            policies={"policyA": {"tok1": 1, "tok2": 1}},
        )
        assert _estimate_value_cbor_bytes(out["value"]) < _DOS_VALUE_CBOR_MIN
        assert scorer.gate(_features([out])) is False

    def test_high_pair_bundle_passes_pair_branch(self, scorer):
        # >= dos_asset_min (15) distinct pairs is the canonical DoS shape.
        policies = {f"policy{i:03d}": {"x": 1} for i in range(15)}
        out = _make_output(SCRIPT_ADDR, policies=policies)
        assert scorer.gate(_features([out])) is True

    def test_high_cbor_low_pair_passes_byte_branch(self, scorer):
        # Long-asset-name evasion: fewer than 15 pairs, but the serialized
        # Value CBOR crosses dos_value_cbor_min. The byte branch must engage so
        # an attacker cannot dodge the pair-count branch with verbose names.
        policies = {f"{i:056d}": {("a" * 64): 1} for i in range(10)}
        out = _make_output(SCRIPT_ADDR, policies=policies)
        _, token_count = (None, sum(len(v) for v in policies.values()))
        assert token_count < 15
        assert _estimate_value_cbor_bytes(out["value"]) >= _DOS_VALUE_CBOR_MIN
        assert scorer.gate(_features([out])) is True


class TestScore:
    def test_many_assets_high_score(self, scorer):
        """20 distinct tokens from 5 policies should score high."""
        policies = {}
        for i in range(5):
            policies[f"policy{i:02d}"] = {f"token{j}": 1 for j in range(4)}
        out = _make_output(SCRIPT_ADDR, lovelace=1_500_000, policies=policies)
        result = scorer.score(_features([out]))
        assert result.score > 40
        assert result.sub_scores["unique_assetclass_count"] > 0.5

    def test_single_asset_low_score(self, scorer):
        out = _make_output(SCRIPT_ADDR, lovelace=10_000_000, policies={"p": {"t": 100}})
        result = scorer.score(_features([out]))
        assert result.score < 30

    def test_low_ada_boosts_score(self, scorer):
        """Minimum ADA with many assets: inverted ADA sub-score should be high."""
        policies = {f"p{i}": {f"t{j}": 1 for j in range(3)} for i in range(3)}
        out = _make_output(SCRIPT_ADDR, lovelace=1_200_000, policies=policies)
        result = scorer.score(_features([out]))
        assert result.sub_scores["lovelace_inverted"] > 0.5

    def test_max_across_outputs(self, scorer):
        """Score should be the max across multiple eligible outputs."""
        low = _make_output(SCRIPT_ADDR, lovelace=50_000_000, policies={"p": {"t": 1}})
        high = _make_output(
            SCRIPT_ADDR,
            lovelace=1_200_000,
            policies={f"p{i}": {f"t{j}": 1 for j in range(5)} for i in range(4)},
        )
        result = scorer.score(_features([low, high]))
        single = scorer.score(_features([high]))
        assert result.score == single.score

    def test_value_bloat_dos_composite_reason(self, scorer):
        """When all three primary signals saturate at a script address, the
        composite ``script_value_bloat_dos`` reason fires. Canonical
        value-bloat DoS signature: many unique policies, large value CBOR,
        minimal lovelace. Distinguishes a contract-DoS shape from generic
        dust spam without renaming the class column."""
        # 80 unique (policy, name) pairs to mirror the canonical mint.
        policies = {f"policy{i:03d}": {"x": 1} for i in range(80)}
        out = _make_output(SCRIPT_ADDR, lovelace=1_200_000, policies=policies)
        result = scorer.score(_features([out]))
        assert "script_value_bloat_dos" in result.reasons
        # Sanity: the three primary signals must all be present too.
        assert "high_value_cbor_bytes" in result.reasons
        assert "many_distinct_assets" in result.reasons
        assert "low_lovelace_amount" in result.reasons

    def test_no_composite_reason_when_one_signal_missing(self, scorer):
        """Generous ada cushion should suppress lovelace_inverted and
        therefore suppress the composite reason even with many assets.

        Uses 1000 ADA so the margin against any plausible upward baseline
        rebase stays comfortable; if this flips in the future the bootstrap
        ``ada_amount.p99`` has been raised dramatically and the test is no
        longer the right test."""
        policies = {f"p{i:03d}": {"x": 1} for i in range(20)}
        out = _make_output(SCRIPT_ADDR, lovelace=1_000_000_000, policies=policies)
        result = scorer.score(_features([out]))
        assert "script_value_bloat_dos" not in result.reasons


class TestNetworkScopedAllowlist:
    """Allowlist suppression is keyed by network so a preprod entry cannot
    silently disable mainnet alerts (and vice versa).

    Tests inject allowlist entries via the module's parsed maps rather
    than mutating YAML; the helper restores the originals to keep test
    ordering independent.
    """

    def _patch_allowlist(self, monkeypatch, policies=None, prefixes=None):
        from app.analysis.scorers import token_dust as tdm

        if policies is not None:
            monkeypatch.setattr(tdm, "_ALLOWLIST_POLICIES", policies)
        if prefixes is not None:
            monkeypatch.setattr(tdm, "_ALLOWLIST_PREFIXES", prefixes)

    def test_policy_allowlist_suppresses_matching_network(self, scorer, monkeypatch):
        self._patch_allowlist(
            monkeypatch,
            policies={"preprod": frozenset({"protocol_policy"})},
        )
        out = _make_output(
            SCRIPT_ADDR,
            lovelace=2_000_000,
            policies={"protocol_policy": {"tok1": 1, "tok2": 1, "tok3": 1}},
        )
        result = scorer.score(_features([out]))
        assert result.score == 0.0
        assert result.reasons == []

    def test_policy_allowlist_does_not_cross_networks(self, scorer, monkeypatch):
        # The same policy ID allowlisted on mainnet must NOT suppress a
        # preprod tx; otherwise a permissionless preprod attacker could
        # mint under the hash and bypass detection.
        self._patch_allowlist(
            monkeypatch,
            policies={"mainnet": frozenset({"protocol_policy"})},
        )
        out = _make_output(
            SCRIPT_ADDR,
            lovelace=2_000_000,
            policies={"protocol_policy": {"tok1": 1, "tok2": 1, "tok3": 1}},
        )
        result = scorer.score(_features([out]))
        # Detection still runs; high asset count + low ADA produces a real score.
        assert result.score > 0.0

    def test_mixed_policy_bundle_is_not_allowlisted(self, scorer, monkeypatch):
        # Bundle contains an allowlisted policy AND an attacker-controlled
        # one; the scorer must continue. Otherwise an attacker could smuggle
        # dust inside a known-protocol UTxO.
        self._patch_allowlist(
            monkeypatch,
            policies={"preprod": frozenset({"protocol_policy"})},
        )
        out = _make_output(
            SCRIPT_ADDR,
            lovelace=2_000_000,
            policies={
                "protocol_policy": {"tok1": 1, "tok2": 1},
                "attacker_policy": {"dust_a": 1, "dust_b": 1, "dust_c": 1},
            },
        )
        result = scorer.score(_features([out]))
        assert result.score > 0.0


class TestDosAssetThresholdDiscriminator:
    """``dos_asset_min`` separates real value-bloat DoS from legitimate
    multi-asset protocol UTxOs by total distinct (policy, name) pairs.

    Real DoS exploits force the contract to carry many pairs (CTF 06: 80
    total). Protocol multi-asset UTxOs bundle few pairs by design (Lenfi
    loan offer: ~4 pairs across 3 policies). The threshold catches the
    structural difference without relying on per-protocol allowlists or
    per-script baselines. Robust to both one-policy-many-names and
    many-policies-few-names DoS shapes because both add equivalent CBOR
    overhead.
    """

    def test_high_asset_count_one_policy_fires_composite_reason(self, scorer):
        # CTF 06 shape: 80 names under a single one-shot policy, low ADA.
        # Use 1_200_000 (the bootstrap p50) so lovelace_inverted saturates.
        many_names = {f"asset{i:03d}": 1 for i in range(80)}
        out = _make_output(
            SCRIPT_ADDR,
            lovelace=1_200_000,
            policies={"oneshot_dos_policy": many_names},
        )
        result = scorer.score(_features([out]))
        assert "script_value_bloat_dos" in result.reasons
        assert result.sub_scores["max_assets_per_policy"] == 80.0
        # One-policy shape produces less CBOR overhead than the
        # many-policies symmetric shape (single policy header instead of
        # 80), so the bytes axis is partial; High band suffices to
        # prove the discriminator preserved the alert.
        assert result.score >= BAND_HIGH_THRESHOLD  # High or Critical

    def test_high_asset_count_many_policies_fires_composite_reason(self, scorer):
        # Symmetric DoS shape: 80 one-shot policies x 1 name each. Same
        # CBOR overhead order; the discriminator must catch this too.
        policies = {f"policy{i:03d}": {"x": 1} for i in range(80)}
        out = _make_output(SCRIPT_ADDR, lovelace=1_200_000, policies=policies)
        result = scorer.score(_features([out]))
        assert "script_value_bloat_dos" in result.reasons
        assert result.score >= SYMMETRIC_DOS_MIN_SCORE

    def test_low_asset_count_caps_at_moderate(self, scorer):
        # Lenfi-style shape: 4 pairs across 3 policies. No structural
        # ability to bloat tx CBOR. Composite reason must NOT fire;
        # band capped at Moderate. Low ADA so the other axes saturate
        # and prove the cap is what stops the band (not weak signals).
        out = _make_output(
            SCRIPT_ADDR,
            lovelace=1_200_000,
            policies={
                "policy_a": {"offer_nft": 1, "ref_nft": 1},
                "policy_b": {"loan_token": 1},
                "policy_c": {"lend_batch_unit": 50},
            },
        )
        result = scorer.score(_features([out]))
        assert "script_value_bloat_dos" not in result.reasons
        assert result.sub_scores["max_assets_per_policy"] == 2.0
        assert result.score < BAND_HIGH_THRESHOLD  # capped below High
