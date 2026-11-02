"""Poll bounds of both scoring lanes, and baseline-cache behavior (WS4)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.db import clickhouse

# Captured at collection time, BEFORE the autouse conftest fixture patches
# app.db.clickhouse.get_baseline with a Mock: the cache tests need the real
# implementation.
_real_get_baseline = clickhouse.get_baseline

TS = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
UNTIL = TS + timedelta(days=1)
# Every read of the poll's first step that takes the range: transactions and
# the scores side's own transactions.
_RANGED_READS = 2


@pytest.fixture(autouse=True)
def _clean_state():
    clickhouse._baseline_cache_clear()
    yield
    clickhouse._baseline_cache_clear()


def _hydrated(tx_hash):
    """A hydrate-step row: tx_hash first, then the 14 other selected columns."""
    return (tx_hash, "preprod", *([None] * 12), TS)


class TestUnanalyzedPollBounds:
    """A poll's reads follow its range, never the backlog or the history."""

    def _capture(self, monkeypatch, *responses):
        captured = []
        queue = list(responses)
        fake = MagicMock()

        def _execute(sql, params=None):
            captured.append((sql, params))
            return queue.pop(0) if queue else []

        fake.execute.side_effect = _execute
        monkeypatch.setattr(clickhouse, "_get_client", lambda: fake)
        return captured

    def test_live_poll_bounds_every_read_by_since(self, monkeypatch):
        captured = self._capture(monkeypatch)
        clickhouse.get_unanalyzed_transactions("preprod", 50, since=TS, newest_first=True)
        sql, params = captured[0]
        assert params["since"] == TS
        assert sql.count("ingestion_timestamp >= %(since)s") == _RANGED_READS
        assert "analyzed_at >= %(since)s" in sql
        # The live window is open-ended and served newest first.
        assert "%(until)s" not in sql
        assert "ORDER BY ingested DESC" in sql

    def test_backlog_poll_bounds_every_read_by_until(self, monkeypatch):
        captured = self._capture(monkeypatch)
        clickhouse.get_unanalyzed_transactions("preprod", 20, since=TS, until=UNTIL)
        sql, params = captured[0]
        assert params["until"] == UNTIL
        assert sql.count("ingestion_timestamp < %(until)s") == _RANGED_READS
        assert "ORDER BY ingested ASC" in sql

    def test_the_scores_side_reads_only_the_ranges_transactions(self, monkeypatch):
        # Without it, a range far in the past would read every score since.
        captured = self._capture(monkeypatch)
        clickhouse.get_unanalyzed_transactions("preprod", 20, since=TS, until=UNTIL)
        sql, _ = captured[0]
        scores_side = sql[sql.index("FROM tx_class_scores") :]
        assert "tx_hash IN (" in scores_side.split("transaction_inputs")[0]

    def test_the_pick_reads_no_payload(self, monkeypatch):
        # raw_data for every unscored row is what made the old poll read the
        # whole backlog's payload behind a large backlog.
        captured = self._capture(monkeypatch)
        clickhouse.get_unanalyzed_transactions("preprod", 50, since=TS)
        sql, _ = captured[0]
        assert "raw_data" not in sql
        assert "metadata" not in sql

    def test_an_empty_pick_skips_the_hydrate_read(self, monkeypatch):
        captured = self._capture(monkeypatch, [])
        assert clickhouse.get_unanalyzed_transactions("preprod", 50, since=TS) == []
        assert len(captured) == 1

    def test_the_hydrate_reads_the_batch_by_key_in_pick_order(self, monkeypatch):
        picked = [("b" * 64, TS), ("a" * 64, TS), ("c" * 64, TS)]
        # "c" merged away between the two reads: skipped, polled again later.
        captured = self._capture(monkeypatch, picked, [_hydrated("a" * 64), _hydrated("b" * 64)])
        rows = clickhouse.get_unanalyzed_transactions("preprod", 50, since=TS, until=UNTIL)
        assert [r["tx_hash"] for r in rows] == ["b" * 64, "a" * 64]
        sql, params = captured[1]
        assert "raw_data" in sql
        assert "tx_hash IN %(hashes)s" in sql
        assert "LIMIT 1 BY tx_hash" in sql
        assert params["hashes"] == ["b" * 64, "a" * 64, "c" * 64]
        assert "ingestion_timestamp < %(until)s" in sql

    def test_earliest_ingestion_is_none_on_an_empty_table(self, monkeypatch):
        # min() over no rows is 1970, not NULL: the count decides.
        self._capture(monkeypatch, [(datetime(1970, 1, 1), 0)])
        assert clickhouse.earliest_ingestion("preprod") is None

    def test_earliest_ingestion_is_utc_aware(self, monkeypatch):
        self._capture(monkeypatch, [(datetime(2026, 7, 15, 8, 0, 0), 12)])
        assert clickhouse.earliest_ingestion("preprod") == datetime(
            2026, 7, 15, 8, 0, 0, tzinfo=UTC
        )


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
