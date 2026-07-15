"""Watermark cursor, poll bounds, and baseline-cache behavior (WS4)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.analysis import engine
from app.config import settings
from app.db import clickhouse

# Captured at collection time, BEFORE the autouse conftest fixture patches
# app.db.clickhouse.get_baseline with a Mock: the cache tests need the real
# implementation.
_real_get_baseline = clickhouse.get_baseline

TS = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_state():
    engine._unanalyzed_watermark.clear()
    engine._last_full_rescan.clear()
    clickhouse._baseline_cache_clear()
    yield
    engine._unanalyzed_watermark.clear()
    engine._last_full_rescan.clear()
    clickhouse._baseline_cache_clear()


class TestWatermark:
    def test_first_poll_is_full_rescan(self):
        assert engine._poll_since("preprod") == (None, True)

    def test_subsequent_polls_use_watermark(self):
        import time

        assert engine._poll_since("preprod") == (None, True)
        # The clock is armed by run_once AFTER the rescan succeeds; arm it
        # here the same way (pure _poll_since no longer writes it).
        engine._last_full_rescan["preprod"] = time.monotonic()
        engine._advance_watermark("preprod", [{"ingestion_timestamp": TS}])
        since, full_rescan = engine._poll_since("preprod")
        expected = TS - timedelta(seconds=settings.UNANALYZED_OVERLAP_SECONDS)
        assert since == expected
        assert full_rescan is False

    def test_rescan_interval_forces_full_poll(self, monkeypatch):
        import time

        monkeypatch.setattr(settings, "UNANALYZED_FULL_RESCAN_INTERVAL_SECONDS", 0)
        engine._poll_since("preprod")
        engine._last_full_rescan["preprod"] = time.monotonic()
        engine._advance_watermark("preprod", [{"ingestion_timestamp": TS}])
        # Interval of 0: every poll is a full rescan (never-skip guarantee).
        assert engine._poll_since("preprod") == (None, True)

    def test_failed_rescan_retried_on_next_poll(self):
        # The clock is NOT armed by _poll_since itself: a rescan that
        # crashes mid-batch must re-run on the next poll, not wait out a
        # whole interval (review finding).
        assert engine._poll_since("preprod") == (None, True)
        assert engine._poll_since("preprod") == (None, True)

    def test_watermark_never_regresses(self):
        engine._advance_watermark("preprod", [{"ingestion_timestamp": TS}])
        engine._advance_watermark(
            "preprod",
            [{"ingestion_timestamp": TS - timedelta(hours=1)}],
        )
        expected = TS - timedelta(seconds=settings.UNANALYZED_OVERLAP_SECONDS)
        assert engine._unanalyzed_watermark["preprod"] == expected


class TestUnanalyzedPollBounds:
    def _capture(self, monkeypatch):
        captured = []
        fake = MagicMock()
        fake.execute.side_effect = lambda sql, params=None: captured.append((sql, params)) or []
        monkeypatch.setattr(clickhouse, "_get_client", lambda: fake)
        return captured

    def test_since_bounds_all_three_sides(self, monkeypatch):
        captured = self._capture(monkeypatch)
        clickhouse.get_unanalyzed_transactions("preprod", 50, since=TS)
        sql, params = captured[0]
        assert params["since"] == TS
        assert "t.ingestion_timestamp >= %(since)s" in sql
        assert "analyzed_at >= %(since)s" in sql
        # The inputs-visibility guard subquery is bounded too.
        assert sql.count("ingestion_timestamp >= %(since)s") == 2

    def test_no_since_keeps_full_scan_shape(self, monkeypatch):
        captured = self._capture(monkeypatch)
        clickhouse.get_unanalyzed_transactions("preprod", 50)
        sql, params = captured[0]
        assert "since" not in (params or {})
        assert "%(since)s" not in sql


class TestBaselineCache:
    def _client(self, monkeypatch, rows):
        fake = MagicMock()
        fake.execute.return_value = rows
        monkeypatch.setattr(clickhouse, "_get_client", lambda: fake)
        return fake

    def test_second_lookup_hits_cache(self, monkeypatch):
        fake = self._client(monkeypatch, [(1.0, 9.0, 300, None, 90)])
        a = _real_get_baseline("preprod", "per_script", "addr", "f")
        b = _real_get_baseline("preprod", "per_script", "addr", "f")
        assert a == b
        assert fake.execute.call_count == 1

    def test_negative_results_cached(self, monkeypatch):
        fake = self._client(monkeypatch, [])
        assert _real_get_baseline("preprod", "per_script", "addr", "f") is None
        assert _real_get_baseline("preprod", "per_script", "addr", "f") is None
        assert fake.execute.call_count == 1

    def test_insert_baselines_invalidates(self, monkeypatch):
        fake = self._client(monkeypatch, [(1.0, 9.0, 300, None, 90)])
        _real_get_baseline("preprod", "per_script", "addr", "f")
        clickhouse.insert_baselines(
            [("preprod", "per_script", "addr", "f", 1.0, 12.0, 300, TS, 90)],
        )
        _real_get_baseline("preprod", "per_script", "addr", "f")
        # 1 select + 1 insert + 1 fresh select after invalidation
        assert fake.execute.call_count == 3

    def test_cache_returns_copies(self, monkeypatch):
        self._client(monkeypatch, [(1.0, 9.0, 300, None, 90)])
        a = _real_get_baseline("preprod", "per_script", "addr", "f")
        a["p99"] = 999.0  # caller mutation must not poison the cache
        b = _real_get_baseline("preprod", "per_script", "addr", "f")
        assert b["p99"] == 9.0
