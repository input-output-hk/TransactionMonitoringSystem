"""Token-registry refresh resilience.

A registry outage, total (0 entries fetched) or PARTIAL (a few subjects
served, most failing), must not REPLACE a previously complete cache with a
smaller merge: that silently shrank fake_token impersonation coverage for a
full refresh interval (review findings). Keeping a stale superset only
risks false positives on delisted tokens, the safe direction.
"""

import pytest

from app.analysis import external


@pytest.fixture(autouse=True)
def _clean_cache():
    external._cache.pop("legitimate_tokens", None)
    yield
    external._cache.pop("legitimate_tokens", None)


class TestRegistryOutage:
    def test_outage_keeps_previous_full_cache(self, monkeypatch):
        full = {f"TOKEN{i}": ["p" * 56] for i in range(50)}
        # Stale entry (ts=0): the refresh runs BECAUSE the TTL expired, so
        # the guard must read the raw cache, not the TTL-checked accessor.
        external._cache["legitimate_tokens"] = {"data": full, "ts": 0}
        monkeypatch.setattr(
            external, "_refresh_legitimate_tokens",
            lambda: (dict(external._SEED_TOKENS), 0),
        )
        count = external.refresh_token_registry()
        assert count == 50
        assert external._cache["legitimate_tokens"]["data"] == full

    def test_cold_start_outage_still_caches_seeds(self, monkeypatch):
        monkeypatch.setattr(
            external, "_refresh_legitimate_tokens",
            lambda: (dict(external._SEED_TOKENS), 0),
        )
        count = external.refresh_token_registry()
        assert count == len(external._SEED_TOKENS)
        assert external._cache["legitimate_tokens"]["data"]

    def test_successful_fetch_overwrites(self, monkeypatch):
        external._cache["legitimate_tokens"] = {"data": {"OLD": ["p"]}, "ts": 0}
        fresh = {f"NEW{i}": ["q" * 56] for i in range(10)}
        monkeypatch.setattr(
            external, "_refresh_legitimate_tokens", lambda: (fresh, 10),
        )
        count = external.refresh_token_registry()
        assert count == 10
        assert external._cache["legitimate_tokens"]["data"] == fresh

    def test_partial_outage_keeps_previous_full_cache(self, monkeypatch):
        # The registry serves 1 of ~31 subjects: fetched > 0 used to bypass
        # the outage guard and replace a complete cache with a
        # near-seeds-only one, shrinking impersonation coverage for 24h.
        full = {f"TOKEN{i}": ["p" * 56] for i in range(50)}
        external._cache["legitimate_tokens"] = {"data": full, "ts": 0}
        shrunken = dict(external._SEED_TOKENS)
        shrunken["ONLYONE"] = ["r" * 56]
        assert len(shrunken) < len(full)
        monkeypatch.setattr(
            external, "_refresh_legitimate_tokens", lambda: (shrunken, 1),
        )
        count = external.refresh_token_registry()
        assert count == 50
        assert external._cache["legitimate_tokens"]["data"] == full

    def test_grown_registry_replaces_cache(self, monkeypatch):
        # A legitimately grown registry (fetched > 0, result not smaller
        # than the cache) must still replace, or the cache would fossilise.
        previous = {f"TOKEN{i}": ["p" * 56] for i in range(10)}
        external._cache["legitimate_tokens"] = {"data": previous, "ts": 0}
        grown = {f"TOKEN{i}": ["p" * 56] for i in range(12)}
        monkeypatch.setattr(
            external, "_refresh_legitimate_tokens", lambda: (grown, 12),
        )
        count = external.refresh_token_registry()
        assert count == 12
        assert external._cache["legitimate_tokens"]["data"] == grown
