"""Unit tests for the Token Dust scorer (Class 1)."""

import pytest

from app.analysis.features import _estimate_value_cbor_bytes
from app.analysis.normalise import (
    BAND_HIGH_THRESHOLD,
    BAND_MODERATE_MAX,
    BAND_MODERATE_THRESHOLD,
)
from app.analysis.scorer_config import get as _get_cfg
from app.analysis.scorers.token_dust import _DOS_VALUE_CBOR_MIN, TokenDustScorer
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


class TestShippedMainnetAllowlist:
    """The five mainnet collection policies shipped in detection.yaml
    (verified 2026-07-20 by adversarial triage of the July mainnet
    token_dust High alerts). These read the parsed module maps, so a YAML
    edit that breaks or truncates an entry fails here, not silently in
    production.
    """

    def _mainnet_features(self, outputs):
        # features_for_outputs pins preprod; the shipped entries are
        # mainnet-scoped, so build the dict for the mainnet network.
        return {
            "tx_hash": "shipped-allowlist-test",
            "network": "mainnet",
            "raw_data": {"outputs": outputs},
        }

    def test_shipped_mainnet_policies_are_well_formed(self):
        # A truncated or typo'd hash would never match on-chain policy ids
        # and leave the FP cluster alive with no error anywhere.
        from app.analysis.scorers.token_dust import _ALLOWLIST_POLICIES

        shipped = _ALLOWLIST_POLICIES.get("mainnet", frozenset())
        assert shipped, "expected the five verified mainnet collection policies"
        for policy in shipped:
            assert len(policy) == 56, f"policy id must be 28 bytes hex: {policy}"
            int(policy, 16)  # raises on non-hex

    def test_shipped_mainnet_policies_suppress_pure_bundle(self, scorer):
        # A bundle made exclusively of the verified collections (the L4VA
        # vault / CSWAP pool / Wayup listing shape) is suppressed on mainnet.
        from app.analysis.scorers.token_dust import _ALLOWLIST_POLICIES

        shipped = sorted(_ALLOWLIST_POLICIES.get("mainnet", frozenset()))
        policies = {p: {f"tok{i:02d}": 1 for i in range(4)} for p in shipped}
        out = _make_output(SCRIPT_ADDR, lovelace=2_000_000, policies=policies)
        result = scorer.score(self._mainnet_features([out]))
        assert result.score == 0.0
        assert result.reasons == []

    def test_shipped_allowlist_plus_attacker_policy_still_fires(self, scorer):
        # Recall guard: smuggling attacker dust alongside the verified
        # collections un-allowlists the bundle and the scorer runs.
        from app.analysis.scorers.token_dust import _ALLOWLIST_POLICIES

        shipped = sorted(_ALLOWLIST_POLICIES.get("mainnet", frozenset()))
        policies = {p: {f"tok{i:02d}": 1 for i in range(4)} for p in shipped}
        policies["a" * 56] = {f"dust{i:02d}": 1 for i in range(3)}
        out = _make_output(SCRIPT_ADDR, lovelace=2_000_000, policies=policies)
        result = scorer.score(self._mainnet_features([out]))
        assert result.score > 0.0


class TestEstablishedCollectionCap:
    """Established-collection Moderate cap (token_dust.established_collection).

    A bundle made entirely of policies first seen >= min_policy_age_slots
    ago, with nothing minted in this tx, is the NFT-protocol bulk shape
    (vault rollover, NFT AMM pool, bulk listing) and is capped at the top
    of Moderate: recorded, never suppressed, never High. Every
    degraded-data path must fail open (no cap), which is what keeps the
    CTF-06 fresh-policy attack pins intact.
    """

    _SLOT = 200_000_000
    _MIN_AGE = int(_get_cfg("token_dust")["established_collection"]["min_policy_age_slots"])

    def _dust_features(self, policies, slot=_SLOT, mint=None):
        out = _make_output(SCRIPT_ADDR, lovelace=1_200_000, policies=policies)
        feats = _features([out])
        feats["slot"] = slot
        if mint is not None:
            feats["raw_data"]["mint"] = mint
        return feats

    def _eighty_pair_bundle(self, policy="e" * 56):
        # The CTF-06 shape: 80 distinct names under one policy.
        return {policy: {f"{i:03d}{'ab' * 6}": 1 for i in range(80)}}

    def _patch_first_seen(self, monkeypatch, mapping):
        from app.db import clickhouse

        monkeypatch.setattr(clickhouse, "get_policies_first_seen", lambda net, pids: mapping)

    def test_established_collections_cap_to_moderate(self, scorer, monkeypatch):
        policy = "e" * 56
        self._patch_first_seen(monkeypatch, {policy: self._SLOT - (self._MIN_AGE + 1)})
        result = scorer.score(self._dust_features(self._eighty_pair_bundle(policy)))
        # Capped, not suppressed: band drops out of High but the finding,
        # its saturated sub-scores, and the composite reason all stand.
        assert BAND_MODERATE_THRESHOLD < result.score <= BAND_MODERATE_MAX
        assert "script_value_bloat_dos" in result.reasons
        assert "established_collection_cap" in result.reasons
        assert result.evidence["policy_ages_known"] is True
        assert result.evidence["min_policy_age_slots_observed"] == self._MIN_AGE + 1

    def test_fresh_policy_not_capped(self, scorer, monkeypatch):
        # ATTACK-MUST-FIRE twin of the CTF-06 pin: a policy first seen
        # minutes ago is exactly the mint-and-fire dust campaign.
        policy = "e" * 56
        self._patch_first_seen(monkeypatch, {policy: self._SLOT - 600})
        result = scorer.score(self._dust_features(self._eighty_pair_bundle(policy)))
        assert result.score >= BAND_HIGH_THRESHOLD
        assert "established_collection_cap" not in result.reasons

    def test_minted_in_tx_never_capped(self, scorer, monkeypatch):
        # ATTACK-MUST-FIRE: an old OPEN policy minting fresh junk names in
        # this very tx must stay uncapped regardless of policy age.
        policy = "e" * 56
        self._patch_first_seen(monkeypatch, {policy: self._SLOT - (self._MIN_AGE * 10)})
        result = scorer.score(
            self._dust_features(
                self._eighty_pair_bundle(policy),
                mint={policy: {"deadbeef": 80}},
            )
        )
        assert result.score >= BAND_HIGH_THRESHOLD
        assert "established_collection_cap" not in result.reasons

    def test_partial_age_data_fails_open(self, scorer, monkeypatch):
        # Two policies in the bundle, only one has a first-seen row: age is
        # not proven for the whole bundle, so no cap.
        p_known, p_unknown = "e" * 56, "f" * 56
        self._patch_first_seen(monkeypatch, {p_known: self._SLOT - (self._MIN_AGE * 10)})
        policies = {
            p_known: {f"{i:03d}{'ab' * 6}": 1 for i in range(40)},
            p_unknown: {f"{i:03d}{'cd' * 6}": 1 for i in range(40)},
        }
        result = scorer.score(self._dust_features(policies))
        assert result.score >= BAND_HIGH_THRESHOLD
        assert "established_collection_cap" not in result.reasons
        assert result.evidence["policy_ages_known"] is False

    def test_missing_slot_fails_open(self, scorer, monkeypatch):
        policy = "e" * 56
        self._patch_first_seen(monkeypatch, {policy: 1})
        result = scorer.score(self._dust_features(self._eighty_pair_bundle(policy), slot=None))
        assert result.score >= BAND_HIGH_THRESHOLD
        assert "established_collection_cap" not in result.reasons

    def test_lookup_exception_fails_open(self, scorer, monkeypatch):
        from app.db import clickhouse

        def _boom(net, pids):
            raise RuntimeError("clickhouse down")

        monkeypatch.setattr(clickhouse, "get_policies_first_seen", _boom)
        result = scorer.score(self._dust_features(self._eighty_pair_bundle()))
        assert result.score >= BAND_HIGH_THRESHOLD
        assert "established_collection_cap" not in result.reasons
