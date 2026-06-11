"""Token-registry refresh resilience.

A total registry outage (0 entries fetched) must not REPLACE a previously
complete cache with the seeds-only merge: that silently shrank fake_token
impersonation coverage for a full refresh interval (review finding).
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
