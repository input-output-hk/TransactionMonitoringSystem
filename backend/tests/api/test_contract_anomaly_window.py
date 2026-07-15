"""Regression: the contract_anomaly list filters analyzed_at IN PYTHON (the
synthetic class has no DB column), unlike the stored classes which filter in
ClickHouse. So it must tolerate the tz-aware bounds the frontend sends
(Z-suffixed ISO -> FastAPI parses aware) against ClickHouse's naive-UTC
analyzed_at. Before the fix, comparing naive vs aware raised TypeError, which the
endpoint swallowed into an empty page (the "contract_anomaly shows nothing when a
date filter is applied" bug)."""

from datetime import UTC, datetime

from app.api import contract_anomaly_read as car
from app.api.contract_anomaly_read import _within_analyzed_window

# ClickHouse returns naive-UTC datetimes for analyzed_at.
_NAIVE_IN = datetime(2026, 6, 1, 12, 0, 0)
# The frontend sends Z-suffixed bounds -> FastAPI parses them tz-aware.
_AWARE_FROM = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
_AWARE_TO = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)


def test_naive_value_with_aware_bounds_does_not_raise_and_matches():
    # The exact crashing combination: naive column value, aware bounds.
    assert _within_analyzed_window(_NAIVE_IN, _AWARE_FROM, _AWARE_TO) is True


def test_naive_value_before_aware_from_is_excluded():
    assert _within_analyzed_window(datetime(2026, 3, 1, 0, 0, 0), _AWARE_FROM, _AWARE_TO) is False


def test_naive_value_at_or_after_aware_to_is_excluded():
    # Upper bound is exclusive (mirrors the SQL `< to`).
    assert _within_analyzed_window(_AWARE_TO.replace(tzinfo=None), _AWARE_FROM, _AWARE_TO) is False


def test_aware_value_with_naive_bounds_also_safe():
    # The reverse mix must not raise either.
    aware_in = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert _within_analyzed_window(aware_in, datetime(2026, 4, 1), datetime(2026, 7, 1)) is True


def test_no_bounds_matches_any_value():
    assert _within_analyzed_window(_NAIVE_IN, None, None) is True


def test_none_value_only_matches_when_no_bounds():
    assert _within_analyzed_window(None, None, None) is True
    assert _within_analyzed_window(None, _AWARE_FROM, None) is False


def _stored_row(tx_hash: str, analyzed_at: datetime) -> dict:
    """A minimal tx_class_scores row with a LOW 9-class score, so a malicious
    sidecar verdict projects above it and flips max_class to contract_anomaly."""
    return {
        "tx_hash": tx_hash,
        "network": "preprod",
        "max_score": 5.0,
        "max_class": "token_dust",
        "risk_band": "Informational",
        "sub_scores": {},
        "evidence": {},
        "analysis_version": "test",
        "analyzed_at": analyzed_at,  # NAIVE, as ClickHouse returns it
        "corroboration_count": 0,
        "corroborating_classes": "",
    }


async def test_list_with_aware_bounds_returns_rows_end_to_end(monkeypatch):
    """Endpoint-level regression: _list_contract_anomaly_results must not empty
    out when the API passes tz-aware bounds (Z-suffixed) against naive
    analyzed_at. Before the fix this raised TypeError, which analysis.py's
    catch-all swallowed into an empty page."""
    flagged = {
        "txA": [
            {
                "verdict": "malicious",
                "consensus": 1.0,
                "iso_score": 0.9,
                "lof_score": 0.9,
                "votes": 3,
                "target": "addr_test_watched",
                "scored_at": _NAIVE_IN,
                "published_at": _NAIVE_IN,
            }
        ]
    }

    async def fake_flagged(network, limit=car.clustering_queries._RESCUE_FETCH_CAP):
        return flagged

    async def fake_stored(network, hashes):
        return [_stored_row("txA", _NAIVE_IN)]

    monkeypatch.setattr(car.clustering_queries, "flagged_for_network_async", fake_flagged)
    monkeypatch.setattr(car.clickhouse, "get_class_scores_by_hashes_async", fake_stored)

    page, total = await car._list_contract_anomaly_results(
        "preprod",
        bands=None,
        min_score=1.0,
        analyzed_from=_AWARE_FROM,
        analyzed_to=_AWARE_TO,  # tz-aware, the crashing case
        min_corroboration=0,
        sort="date",
        limit=10,
        offset=0,
    )
    assert total == 1
    assert page[0].tx_hash == "txA"
    assert page[0].max_class == "contract_anomaly"  # verdict flipped it above the stored max
