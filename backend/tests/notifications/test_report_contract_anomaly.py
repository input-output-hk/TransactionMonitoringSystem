"""contract_anomaly counting in the periodic report.

contract_anomaly is the sidecar's read-time-only class (never in
tx_class_scores), so the report's windowed GROUP BY counts it as 0. When the
sidecar is enabled, ``build_periodic_report`` overrides that 0 with the count of
flagged verdicts in the window whose projected band is at/above ``min_band`` —
reusing the same projection the alerts use. It is deliberately NOT folded into
``total_transactions_scored`` / ``alerts_by_band`` (a flagged tx is already
counted there by its 9-class score). These tests pin the window + band filters
and the enable-gate.
"""

from datetime import datetime, timezone

import pytest

from app.analysis import contract_anomaly
from app.config import settings
from app.db import archive_queries, clickhouse_scores, clustering_queries
from app.models.transaction import RiskBand
from app.notifications import reports

pytestmark = pytest.mark.asyncio

_WS = datetime(2026, 6, 1, tzinfo=timezone.utc)
_WE = datetime(2026, 6, 8, tzinfo=timezone.utc)


def _w(band, when):
    """A fake resolved winner (only the fields _count reads)."""
    return {"risk_band": band, "scored_at": when}


@pytest.fixture
def flagged(monkeypatch):
    data = {}

    async def fake_flagged(network, limit=clustering_queries._RESCUE_FETCH_CAP):
        return data

    # resolve() just echoes the tx's single pre-built winner, so the test drives
    # band + scored_at directly and stays independent of the projection floors
    # (those are covered by the projection tests).
    monkeypatch.setattr(clustering_queries, "flagged_for_network_async", fake_flagged)
    monkeypatch.setattr(contract_anomaly, "resolve", lambda rows: rows[0])
    return data


async def test_count_filters_by_window_and_min_band(flagged):
    flagged.update({
        "in_high":  [_w(RiskBand("High"), datetime(2026, 6, 3, tzinfo=timezone.utc))],
        "in_mod":   [_w(RiskBand("Moderate"), datetime(2026, 6, 4, tzinfo=timezone.utc))],
        "in_info":  [_w(RiskBand("Informational"), datetime(2026, 6, 4, tzinfo=timezone.utc))],
        "before":   [_w(RiskBand("Critical"), datetime(2026, 5, 1, tzinfo=timezone.utc))],
        "after":    [_w(RiskBand("Critical"), datetime(2026, 7, 1, tzinfo=timezone.utc))],
        "naive_in": [_w(RiskBand("Critical"), datetime(2026, 6, 5))],  # naive -> UTC
    })
    n = await reports._count_contract_anomaly_in_window("preprod", _WS, _WE, "Moderate")
    # in_high + in_mod + naive_in; excludes Informational (< min), before, after.
    assert n == 3


async def test_count_ignores_non_datetime_scored_at(flagged):
    flagged.update({
        "bad": [_w(RiskBand("Critical"), None)],
        "ok":  [_w(RiskBand("Critical"), datetime(2026, 6, 3, tzinfo=timezone.utc))],
    })
    n = await reports._count_contract_anomaly_in_window("preprod", _WS, _WE, "Moderate")
    assert n == 1


@pytest.fixture
def stub_report_scans(monkeypatch):
    """Neutralise the scorer-side report queries so the test isolates the
    contract_anomaly override."""
    async def zero_counts(network, ws, we, bands):
        return {"total": 0, "by_band": {}, "by_class": {}}

    async def zero_archive(network, date_from=None, date_to=None):
        return 0

    async def no_rows(*a, **k):
        return []

    monkeypatch.setattr(clickhouse_scores, "aggregate_window_counts_async", zero_counts)
    monkeypatch.setattr(archive_queries, "archive_count_async", zero_archive)
    monkeypatch.setattr(clickhouse_scores, "get_class_scores_list_async", no_rows)


_CFG = {"min_band": "Moderate", "attack_classes": "all",
        "window_days": 7, "frequency": "weekly"}


async def test_report_populates_contract_anomaly_when_enabled(monkeypatch, stub_report_scans):
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)

    async def fake_count(network, ws, we, min_band):
        return 7
    monkeypatch.setattr(reports, "_count_contract_anomaly_in_window", fake_count)

    report = await reports.build_periodic_report("preprod", _WS, _WE, _CFG)
    assert report.summary.alerts_by_class["contract_anomaly"] == 7
    # Not folded into transaction/band totals (avoids double-counting a tx).
    assert report.summary.total_transactions_scored == 0
    assert all(v == 0 for v in report.summary.alerts_by_band.values())


async def test_report_skips_contract_anomaly_when_disabled(monkeypatch, stub_report_scans):
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", False)

    async def boom(*a, **k):
        raise AssertionError("must not count contract_anomaly when sidecar is off")
    monkeypatch.setattr(reports, "_count_contract_anomaly_in_window", boom)

    report = await reports.build_periodic_report("preprod", _WS, _WE, _CFG)
    assert report.summary.alerts_by_class["contract_anomaly"] == 0
