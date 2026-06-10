"""Raw-store read-back and the engine's raw_data recovery/deferral."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.db import raw_store
from app.analysis.engine import _resolve_raw_data, _raw_fallback_attempts

TS = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
TX = "ab" * 32


@pytest.fixture(autouse=True)
def _clear_attempts():
    _raw_fallback_attempts.clear()
    yield
    _raw_fallback_attempts.clear()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAW_STORE_PATH", str(tmp_path))
    return tmp_path


class TestReadConfirmed:
    def test_round_trip(self, store):
        payload = {"id": TX, "outputs": [{"address": "addr_test1qq"}]}
        raw_store._write_sync("confirmed", "preprod", TX, payload, TS)
        assert raw_store.read_confirmed("preprod", TX, TS) == payload

    def test_adjacent_day_probe(self, store):
        # Written just before midnight, row timestamp lands on the next day.
        payload = {"id": TX}
        raw_store._write_sync("confirmed", "preprod", TX, payload, TS - timedelta(days=1))
        assert raw_store.read_confirmed("preprod", TX, TS) == payload

    def test_mempool_prefix_probe(self, store):
        payload = {"id": TX}
        raw_store._write_sync("mempool", "preprod", TX, payload, TS)
        assert raw_store.read_confirmed("preprod", TX, TS) == payload

    def test_missing_returns_none(self, store):
        assert raw_store.read_confirmed("preprod", TX, TS) is None


def _row(raw_data, truncated=0):
    return {
        "tx_hash": TX,
        "network": "preprod",
        "raw_data": raw_data,
        "raw_data_truncated": truncated,
        "timestamp": TS,
    }


class TestResolveRawData:
    def test_valid_json_kept(self):
        rows = _resolve_raw_data([_row('{"id": "x"}')], "preprod")
        assert rows[0]["raw_data"] == {"id": "x"}

    def test_truncated_recovers_from_store(self, monkeypatch):
        recovered = {"id": TX, "outputs": []}
        with patch("app.analysis.engine.raw_store.read_confirmed", return_value=recovered):
            rows = _resolve_raw_data([_row("", truncated=1)], "preprod")
        assert rows[0]["raw_data"] == recovered
        assert "raw_data_unavailable" not in rows[0]

    def test_corrupt_legacy_json_recovers_from_store(self, monkeypatch):
        # Legacy mid-JSON truncation: the stored prefix never parses.
        recovered = {"id": TX}
        with patch("app.analysis.engine.raw_store.read_confirmed", return_value=recovered):
            rows = _resolve_raw_data([_row('{"id": "trunca')], "preprod")
        assert rows[0]["raw_data"] == recovered

    def test_unrecoverable_defers_then_degrades(self, monkeypatch):
        monkeypatch.setattr(settings, "RAW_FALLBACK_MAX_ATTEMPTS", 3)
        with patch("app.analysis.engine.raw_store.read_confirmed", return_value=None):
            # First two attempts: deferred (no row returned, no score written).
            assert _resolve_raw_data([_row("", truncated=1)], "preprod") == []
            assert _resolve_raw_data([_row("", truncated=1)], "preprod") == []
            # Attempt budget reached: scored degraded with the marker.
            rows = _resolve_raw_data([_row("", truncated=1)], "preprod")
        assert len(rows) == 1
        assert rows[0]["raw_data"] is None
        assert rows[0]["raw_data_unavailable"] is True
        # Bookkeeping is cleared so a future re-score starts fresh.
        assert _raw_fallback_attempts == {}

    def test_legitimately_absent_raw_data_not_deferred(self):
        # Empty without the truncated flag: the tx never had a payload.
        rows = _resolve_raw_data([_row("", truncated=0)], "preprod")
        assert len(rows) == 1
        assert rows[0]["raw_data"] is None
        assert "raw_data_unavailable" not in rows[0]
