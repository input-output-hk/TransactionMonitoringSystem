"""Regression: every scorer must populate ``evidence`` on a positive case.

Evidence drives the per-attack UI panel in the frontend; if a scorer
forgets to attach it, the detail page degrades silently. These tests
exercise a minimal positive fixture per class and assert the
class-specific keys we wire into the UI.
"""

import pytest

from app.analysis.scorers.circular import CircularScorer
from app.analysis.scorers.fake_token import FakeTokenScorer
from app.analysis.scorers.front_running import FrontRunningScorer
from app.analysis.scorers.large_datum import LargeDatumScorer
from app.analysis.scorers.large_value import LargeValueScorer
from app.analysis.scorers.multiple_sat import MultipleSatScorer
from app.analysis.scorers.phishing import PhishingScorer
from app.analysis.scorers.sandwich import SandwichScorer
from app.analysis.scorers.token_dust import TokenDustScorer

SCRIPT = "addr_test1wz5fxvalex"
FAKE_POLICY = "deadbeef" * 7


def _assert_keys(result, *keys):
    for key in keys:
        assert key in result.evidence, f"missing evidence[{key!r}]; got {sorted(result.evidence)}"


def _gate_and_score(scorer, features):
    """Verify the gate fires for the canonical positive fixture, then score.

    Catches regressions where a gate change filters out the very case the
    evidence test is built around (an evidence-only assertion would still
    pass on an empty ``ScorerResult``).
    """
    assert scorer.gate(features) is True, (
        f"gate rejected the canonical positive fixture for {type(scorer).__name__}"
    )
    return scorer.score(features)


def test_large_datum_evidence():
    out = {
        "address": SCRIPT,
        "value": {"lovelace": 2_000_000},
        "datum": "ff" * 9_000,
    }
    result = _gate_and_score(
        LargeDatumScorer(),
        {"tx_hash": "ld", "network": "preprod", "raw_data": {"outputs": [out]}},
    )
    _assert_keys(
        result,
        "datum_bytes_raw",
        "utxo_total_bytes",
        "datum_type",
        "datum_utxo_ratio",
        "target_script_address",
    )
    assert result.evidence["datum_type"] == "inline"
    assert result.evidence["target_script_address"] == SCRIPT


def test_large_value_evidence():
    out = {
        "address": SCRIPT,
        "value": {"lovelace": 1_500_000, "policy01": {"7465737431": 10**35}},
    }
    result = _gate_and_score(
        LargeValueScorer(),
        {"tx_hash": "lv", "network": "preprod", "raw_data": {"outputs": [out]}},
    )
    _assert_keys(
        result,
        "policy_id",
        "asset_name_hex",
        "asset_name_ascii",
        "max_quantity_raw",
        "quantity_digits_raw",
    )
    assert result.evidence["policy_id"] == "policy01"
    assert result.evidence["asset_name_ascii"] == "test1"


def test_token_dust_evidence():
    policies = {f"p{i:02d}": {f"t{j}": 1 for j in range(4)} for i in range(5)}
    out = {"address": SCRIPT, "value": {"lovelace": 1_500_000, **policies}}
    result = _gate_and_score(
        TokenDustScorer(),
        {"tx_hash": "td", "network": "preprod", "raw_data": {"outputs": [out]}},
    )
    _assert_keys(
        result,
        "unique_asset_count",
        "policy_count",
        "value_cbor_bytes_raw",
        "max_assets_per_policy",
        "target_script_address",
    )
    assert result.evidence["unique_asset_count"] == 20
    assert result.evidence["policy_count"] == 5


def test_multiple_sat_evidence():
    inputs = [
        {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
        {"address": SCRIPT, "value": {"lovelace": 5_000_000}},
    ]
    redeemers = {
        "spend:0": {"executionUnits": {"memory": 1, "cpu": 1}},
        "spend:1": {"executionUnits": {"memory": 1, "cpu": 1}},
    }
    features = {
        "tx_hash": "ms",
        "network": "preprod",
        "sender_recurrence": 0.0,
        "raw_data": {"inputs": inputs, "outputs": [], "redeemers": redeemers},
    }
    result = _gate_and_score(MultipleSatScorer(), features)
    _assert_keys(
        result,
        "n_inputs_same_script",
        "redeemer_count",
        "redeemer_input_ratio",
        "cpu_units_total",
        "value_extracted_lovelace",
        "value_returned_lovelace",
        "target_script_address",
        "lovelace_full_drain",
    )
    # No outputs back to the script means a full lovelace drain.
    assert result.evidence["lovelace_full_drain"] is True


def test_front_running_evidence():
    collision = {
        "counterpart_tx": "other01",
        "shared_inputs": 2,
        "delta_ms": 150.0,
        "outcome": "TX_A_CONFIRMED",
        "counterpart_fee": 210_000,
        "counterpart_ttl": 490,
        "shares_change_address": True,
        "attacker_win_count": 5,
        "tx_role": "TX_B",
    }
    features = {
        "tx_hash": "fr",
        "network": "preprod",
        "fee": 200_000,
        "raw_data": {"timeToLive": 500},
        "collision": collision,
    }
    result = _gate_and_score(FrontRunningScorer(), features)
    _assert_keys(
        result,
        "delta_ms",
        "outcome",
        "tx_role",
        "counterpart_tx_hash",
        "shared_input_count",
        "attacker_win_count",
        "attacker_win_count_24h",
    )
    assert result.evidence["tx_role"] == "TX_B"
    # The fixture doesn't set ``attacker_win_count_24h`` so the scorer
    # defaults to 0; the key must still be present for the UI mapping.
    assert result.evidence["attacker_win_count_24h"] == 0


def test_sandwich_evidence():
    sandwich = {
        "tx_a": "a01",
        "tx_b": "b01",
        "pool_id": "pool01",
        "asset_pair": "ADA/HOSKY",
        "attacker_linked": True,
        "swap_rate_victim": 0.85,
        "swap_rate_baseline": 1.0,
        "price_impact_a": 0.03,
        "profit_b": 1_000_000,
        "attacker_sandwich_count": 4,
        "slot_span": 2,
    }
    result = _gate_and_score(
        SandwichScorer(),
        {"tx_hash": "sw", "network": "preprod", "raw_data": {}, "sandwich": sandwich},
    )
    _assert_keys(
        result,
        "pool_id",
        "asset_pair",
        "tx_a_hash",
        "tx_b_hash",
        "slot_span",
        "swap_rate_victim",
        "swap_rate_baseline",
        "attacker_profit_lovelace",
    )
    assert result.evidence["attacker_profit_lovelace"] == 1_000_000


def test_circular_evidence():
    cycle = {
        "cycle_length": 3,
        "addresses": ["a", "b", "c"],
        "hops": [
            {"address": "a", "amount_lovelace": 10_000_000, "slot": 100},
            {"address": "b", "amount_lovelace": 9_800_000, "slot": 102},
            {"address": "a", "amount_lovelace": 9_700_000, "slot": 105},
        ],
        "amount_similarity": 0.95,
        "net_loss_ratio": 0.03,
        "recurrence_count": 4,
        "recipient_entropy": 0.40,
        "round_amount_flag": True,
        "temporal_concentration": 0.70,
        "mean_inter_hop_delta_slots": 3.0,
        "origin_cluster": "a",
    }
    result = _gate_and_score(
        CircularScorer(),
        {"tx_hash": "ci", "network": "preprod", "raw_data": {}, "cycle": cycle},
    )
    _assert_keys(
        result,
        "cycle_length",
        "net_loss_ratio",
        "hops",
        "first_slot",
        "origin_cluster",
    )
    # The per-hop entries the UI iterates must keep address/amount aligned.
    hops = result.evidence["hops"]
    assert len(hops) == 3
    assert hops[0]["address"] == "a" and hops[0]["amount_lovelace"] == 10_000_000


def test_fake_token_evidence():
    mint = {FAKE_POLICY: {"HOSKY": 10_000}}
    outputs = [
        {"address": f"addr{i}", "value": {"lovelace": 1_500_000}} for i in range(5)
    ]
    features = {
        "tx_hash": "ft",
        "network": "mainnet",
        "metadata": None,
        "raw_data": {"mint": mint, "outputs": outputs},
        "slot": 100_000,
    }
    result = _gate_and_score(FakeTokenScorer(), features)
    _assert_keys(
        result,
        "matched_token",
        "fake_policy_id",
        "fake_asset_name_ascii",
        "legit_policy_ids",
        "recipient_count",
        "unicode_confusables",
    )
    assert result.evidence["fake_policy_id"] == FAKE_POLICY
    assert result.evidence["recipient_count"] == 5


def test_phishing_evidence():
    metadata = {"674": "claim rewards at https://evil.example.xyz now"}
    outputs = [
        {"address": "addr_a", "value": {"lovelace": 1_000_000}},
        {"address": "addr_a", "value": {"lovelace": 1_000_000}},  # duplicate recipient
        {"address": "addr_b", "value": {"lovelace": 1_000_000}},
    ]
    features = {
        "tx_hash": "ph",
        "network": "preprod",
        "metadata": metadata,
        "addresses": [],
        "output_count": 3,
        "raw_data": {"outputs": outputs},
    }
    result = _gate_and_score(PhishingScorer(), features)
    _assert_keys(
        result,
        "severity",
        "se_tier",
        "urls",
        "url_count",
        "recipient_count",
        "metadata_labels",
    )
    # Distinct recipient count must dedupe addresses (2 unique, not 3).
    assert result.evidence["recipient_count"] == 2
    assert "674" in result.evidence["metadata_labels"]
    assert result.evidence["urls"], "expected at least one URL extracted"
    # The fixture contains no Tier-1 / Tier-2 / Tier-3 keywords, so the
    # tier label should be "None" — confirms the classifier doesn't
    # default to a positive tier on benign text.
    assert result.evidence["se_tier"] in {
        "None", "Tier 2: Urgency language", "Tier 3: Brand impersonation",
    }
