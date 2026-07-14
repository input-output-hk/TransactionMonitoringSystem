"""TTL cache around the dashboard stats aggregate.

get_class_scores_stats full-scans tx_class_scores (FINAL) plus a
countDistinct over transactions on every dashboard poll, occupying one of
the three executor workers with cost that grows with history (review
finding). The cache bounds the scan rate; staleness only affects KPI cards.
"""

from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.db import clickhouse_scores


@pytest.fixture(autouse=True)
def _clear_cache():
    clickhouse_scores._stats_cache.clear()
    yield
    clickhouse_scores._stats_cache.clear()


@pytest.fixture
def client(monkeypatch):
    mock = MagicMock()
    # One row shaped like the stats SELECT: totals, band counts, avg, last,
    # then 3 per-class aggregates for every class column.
    base = [10, 1, 2, 3, 4, 50.0, None]
    per_class = [0, 0.0, 0.0] * len(clickhouse_scores._CLASS_COLS)
    mock.execute.return_value = [tuple(base + per_class)]
    monkeypatch.setattr(clickhouse_scores, "_client", lambda: mock)
    monkeypatch.setattr(clickhouse_scores, "get_pending_count", lambda n: 5)
    return mock


class TestStatsCache:
    def test_second_call_inside_ttl_hits_cache(self, client, monkeypatch):
        monkeypatch.setattr(settings, "STATS_CACHE_TTL_SECONDS", 60)
        first = clickhouse_scores.get_class_scores_stats("preprod")
        second = clickhouse_scores.get_class_scores_stats("preprod")
        assert client.execute.call_count == 1
        assert first == second

    def test_expired_ttl_refetches(self, client, monkeypatch):
        monkeypatch.setattr(settings, "STATS_CACHE_TTL_SECONDS", 60)
        clock = {"t": 1000.0}
        monkeypatch.setattr(
            "app.db.clickhouse_scores.time.monotonic",
            lambda: clock["t"],
        )
        clickhouse_scores.get_class_scores_stats("preprod")
        clock["t"] += 61.0
        clickhouse_scores.get_class_scores_stats("preprod")
        assert client.execute.call_count == 2

    def test_zero_ttl_disables_cache(self, client, monkeypatch):
        monkeypatch.setattr(settings, "STATS_CACHE_TTL_SECONDS", 0)
        clickhouse_scores.get_class_scores_stats("preprod")
        clickhouse_scores.get_class_scores_stats("preprod")
        assert client.execute.call_count == 2

    def test_keys_are_independent(self, client, monkeypatch):
        monkeypatch.setattr(settings, "STATS_CACHE_TTL_SECONDS", 60)
        clickhouse_scores.get_class_scores_stats("preprod")
        clickhouse_scores.get_class_scores_stats("preprod", include_archived=True)
        clickhouse_scores.get_class_scores_stats("preview")
        assert client.execute.call_count == 3

    def test_cache_returns_copy(self, client, monkeypatch):
        # A caller mutating the returned dict must not poison the cache:
        # neither at the top level nor through the NESTED per_class dicts,
        # which a shallow dict() copy would share with the cache entry.
        monkeypatch.setattr(settings, "STATS_CACHE_TTL_SECONDS", 60)
        first = clickhouse_scores.get_class_scores_stats("preprod")
        first["pending_count"] = -999
        some_class = next(iter(first["per_class"]))
        first["per_class"][some_class]["scored_count"] = -999
        first["per_class"]["injected_class"] = {"scored_count": -1}
        second = clickhouse_scores.get_class_scores_stats("preprod")
        assert second["pending_count"] == 5
        assert second["per_class"][some_class]["scored_count"] == 0
        assert "injected_class" not in second["per_class"]
        # The fresh (pre-store) return value must not share nested dicts
        # with the cache either: mutate it and re-read.
        second["per_class"][some_class]["max_score"] = 12345.0
        third = clickhouse_scores.get_class_scores_stats("preprod")
        assert third["per_class"][some_class]["max_score"] == 0.0
