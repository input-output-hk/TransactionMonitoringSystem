"""Retention-coupling warnings in apply_retention_ttls.

Every baseline percentile query INNER JOINs transactions for chain-time
windowing, and enrichment resolves parent transactions of any age, so a
retention window shorter than the baseline window silently degrades
detection inputs — the operator must be warned for EVERY coupled knob,
not just the features one (review finding).
"""

import logging
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.db.clickhouse_schema import apply_retention_ttls


@pytest.fixture
def zero_retention(monkeypatch):
    for knob in (
        "CH_RETENTION_DAYS_TRANSACTIONS",
        "CH_RETENTION_DAYS_IO",
        "CH_RETENTION_DAYS_FEATURES",
    ):
        monkeypatch.setattr(settings, knob, 0)


class TestRetentionWarnings:
    def test_transactions_retention_below_window_warns(self, zero_retention, monkeypatch, caplog):
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_TRANSACTIONS", 90)
        with caplog.at_level(logging.WARNING):
            apply_retention_ttls(MagicMock())
        warnings = [r for r in caplog.records if "CH_RETENTION_DAYS_TRANSACTIONS" in r.getMessage()]
        assert len(warnings) == 1
        assert "INNER JOIN" in warnings[0].getMessage()

    def test_io_retention_warns_once_despite_three_tables(
        self, zero_retention, monkeypatch, caplog
    ):
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_IO", 30)
        with caplog.at_level(logging.WARNING):
            apply_retention_ttls(MagicMock())
        warnings = [r for r in caplog.records if "CH_RETENTION_DAYS_IO" in r.getMessage()]
        assert len(warnings) == 1

    def test_features_retention_below_window_warns(self, zero_retention, monkeypatch, caplog):
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_FEATURES", 30)
        with caplog.at_level(logging.WARNING):
            apply_retention_ttls(MagicMock())
        assert any("CH_RETENTION_DAYS_FEATURES" in r.getMessage() for r in caplog.records)

    def test_retention_above_window_no_warning(self, zero_retention, monkeypatch, caplog):
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_TRANSACTIONS", 365)
        with caplog.at_level(logging.WARNING):
            apply_retention_ttls(MagicMock())
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_zero_retention_no_ttl_statements(self, zero_retention, caplog):
        client = MagicMock()
        with caplog.at_level(logging.WARNING):
            apply_retention_ttls(client)
        modify_calls = [c for c in client.execute.call_args_list if "MODIFY TTL" in c.args[0]]
        assert modify_calls == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
