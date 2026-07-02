"""contract_anomaly counting + top-alert fold in the periodic report.

contract_anomaly is the sidecar's read-time-only class (never in
tx_class_scores), so the report's windowed GROUP BY counts it as 0. When the
sidecar is enabled, ``build_periodic_report`` replaces that 0 with the count of
flagged verdicts in the window whose projected band is at/above ``min_band`` and
folds the same findings into ``top_alerts`` (they can never come from the
tx_class_scores query). It is deliberately NOT folded into
``total_transactions_scored`` / ``alerts_by_band`` (a flagged tx is already
counted there by its 9-class score). These tests pin the window + band filters,
the published_at-vs-scored_at window semantics (the relabel recall case), the
top-alert fold, and the enable-gate.
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


def _w(band, scored_at, published_at=None, tx_hash="tx", score=90.0):
    """A fake resolved winner (only the fields the report reads). published_at
    defaults to scored_at (the common, non-relabel case)."""
    return {
        "tx_hash": tx_hash, "risk_band": band, "score": score,
        "scored_at": scored_at,
        "published_at": scored_at if published_at is None else published_at,
    }


@pytest.fixture
def flagged(monkeypatch):
    data = {}

    async def fake_flagged(network, limit=clustering_queries._RESCUE_FETCH_CAP,
                           raise_on_error=False):
        return data

    # resolve() echoes the tx's single pre-built winner, so the test drives band
    # + timestamps directly and stays independent of the projection floors (those
    # are covered by the projection tests). The winner carries published_at via
    # the row itself, which the report also reads off the raw rows.
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
    found = await reports._contract_anomaly_in_window("preprod", _WS, _WE, "Moderate")
    # in_high + in_mod + naive_in; excludes Informational (< min), before, after.
    assert len(found) == 3


async def test_late_published_relabel_is_counted(flagged):
    # Recall case: a tx scored months ago, relabeled malicious THIS window keeps
    # its OLD scored_at but gets a FRESH published_at. Windowing on scored_at
    # alone would drop it from every report though the poller alerted on it.
    flagged.update({
        "relabel": [_w(
            RiskBand("Critical"),
            scored_at=datetime(2026, 1, 1, tzinfo=timezone.utc),      # old
            published_at=datetime(2026, 6, 4, tzinfo=timezone.utc),   # in-window
        )],
    })
    found = await reports._contract_anomaly_in_window("preprod", _WS, _WE, "Moderate")
    assert len(found) == 1


async def test_count_ignores_non_datetime_stamps(flagged):
    flagged.update({
        "bad": [_w(RiskBand("Critical"), scored_at=None, published_at=None)],
        "ok":  [_w(RiskBand("Critical"), datetime(2026, 6, 3, tzinfo=timezone.utc))],
    })
    found = await reports._contract_anomaly_in_window("preprod", _WS, _WE, "Moderate")
    assert len(found) == 1


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

    findings = [
        _w(RiskBand("Critical"), _WS, tx_hash=f"tx{i}", score=90.0 - i)
        for i in range(7)
    ]

    async def fake_findings(network, ws, we, min_band):
        return findings
    monkeypatch.setattr(reports, "_contract_anomaly_in_window", fake_findings)

    report = await reports.build_periodic_report("preprod", _WS, _WE, _CFG)
    assert report.summary.alerts_by_class["contract_anomaly"] == 7
    # Not folded into transaction/band totals (avoids double-counting a tx).
    assert report.summary.total_transactions_scored == 0
    assert all(v == 0 for v in report.summary.alerts_by_band.values())
    # Findings are folded into the top list (they can't come from tx_class_scores).
    ca_top = [a for a in report.top_alerts if a.attack_class == "contract_anomaly"]
    assert ca_top and all(a.tx_hash.startswith("tx") for a in ca_top)


async def test_report_top_alerts_rank_across_both_sources(monkeypatch, stub_report_scans):
    # A high-scoring contract_anomaly must outrank a lower scorer alert in the
    # merged top list (top-N by score across both sources).
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_REPORT_TOP_ALERTS", 1)

    async def one_scorer_row(*a, **k):
        return [{
            "tx_hash": "scorer_lo", "max_class": "phishing", "max_score": 61.0,
            "risk_band": "High", "analyzed_at": _WS,
        }]
    monkeypatch.setattr(clickhouse_scores, "get_class_scores_list_async", one_scorer_row)

    async def fake_findings(network, ws, we, min_band):
        return [_w(RiskBand("Critical"), _WS, tx_hash="ca_hi", score=99.0)]
    monkeypatch.setattr(reports, "_contract_anomaly_in_window", fake_findings)

    report = await reports.build_periodic_report("preprod", _WS, _WE, _CFG)
    assert len(report.top_alerts) == 1
    assert report.top_alerts[0].tx_hash == "ca_hi"          # higher score wins
    assert report.top_alerts[0].attack_class == "contract_anomaly"


async def test_report_skips_contract_anomaly_when_disabled(monkeypatch, stub_report_scans):
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", False)

    async def boom(*a, **k):
        raise AssertionError("must not count contract_anomaly when sidecar is off")
    monkeypatch.setattr(reports, "_contract_anomaly_in_window", boom)

    report = await reports.build_periodic_report("preprod", _WS, _WE, _CFG)
    assert report.summary.alerts_by_class["contract_anomaly"] == 0
