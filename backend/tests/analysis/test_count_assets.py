"""Tests for the burn-token-aware asset counting helper."""

from app.analysis.features import _count_assets


def test_pure_ada_returns_zero():
    assert _count_assets({"lovelace": 2_000_000}) == (0, 0)


def test_single_live_token():
    value = {"lovelace": 1_500_000, "policy1": {"tokenA": 5}}
    policies, tokens = _count_assets(value)
    assert policies == 1
    assert tokens == 1


def test_multiple_live_tokens_same_policy():
    value = {"policy1": {"tokenA": 1, "tokenB": 2, "tokenC": 10}}
    policies, tokens = _count_assets(value)
    assert policies == 1
    assert tokens == 3


def test_burn_only_token_skipped():
    # qty=0 indicates a burn-only leftover; must not inflate the count.
    value = {"policy1": {"tokenA": 0}}
    policies, tokens = _count_assets(value)
    assert policies == 0
    assert tokens == 0


def test_mixed_live_and_burn_counts_only_live():
    value = {"policy1": {"liveToken": 1, "burnedToken": 0}}
    policies, tokens = _count_assets(value)
    assert policies == 1
    assert tokens == 1


def test_negative_qty_counts_as_live():
    # Ogmios mint blocks encode burns as negative quantities; still a real event.
    value = {"policy1": {"burned": -5}}
    policies, tokens = _count_assets(value)
    assert policies == 1
    assert tokens == 1


def test_non_numeric_qty_treated_as_zero():
    value = {"policy1": {"bad": "not-a-number"}}
    policies, tokens = _count_assets(value)
    assert policies == 0
    assert tokens == 0


def test_ada_key_ignored():
    value = {"ada": {"lovelace": 5_000_000}, "policy1": {"tokenA": 1}}
    policies, tokens = _count_assets(value)
    assert policies == 1
    assert tokens == 1
