"""Tests for the contract_anomaly projection (clustering sidecar -> host score).

Covers the pure score mapping, the additive read-time merge (recall-first: it
may only ever RAISE max_score / risk_band and must never mutate the stored
per-tx fields), the real-anomaly-fires guarantee, and the CLUSTERING_ENABLED
gate. All hermetic: no ClickHouse, no sidecar.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.analysis import scorer_config
from app.analysis.contract_anomaly import project_score
from app.analysis.normalise import (
    BAND_CRITICAL_THRESHOLD,
    BAND_HIGH_THRESHOLD,
    score_to_band,
)
from app.api.analysis import _merge_contract_anomaly
from app.models.transaction import ClassScoreResult, RiskBand


def _floors():
    return scorer_config.contract_anomaly_config()["verdict_floors"]


class TestProjectScore:
    def test_malicious_floors_into_critical(self):
        score, band = project_score("malicious", None)
        assert score == _floors()["malicious"]
        assert score >= BAND_CRITICAL_THRESHOLD
        assert band is RiskBand.CRITICAL

    def test_anomaly_floors_into_high(self):
        score, band = project_score("anomaly", 0.0)
        assert score == _floors()["anomaly"]
        assert score >= BAND_HIGH_THRESHOLD
        assert band is RiskBand.HIGH

    def test_safe_verdict_suppresses_despite_high_consensus(self):
        # benign / normal are safe labels: consensus can NEVER raise them off 0.
        for verdict in ("benign", "normal"):
            score, _ = project_score(verdict, 0.95)
            assert score == 0.0, f"{verdict} must suppress regardless of consensus"

    def test_consensus_refines_a_positive_verdict_upward(self):
        # A positive verdict (anomaly, floor 60) is raised by a high consensus.
        scale = scorer_config.contract_anomaly_config()["consensus_scale"]
        score, band = project_score("anomaly", 0.9)  # max(60, 90) = 90
        assert score == pytest.approx(0.9 * scale)
        assert band is RiskBand.CRITICAL

    def test_floor_wins_over_low_consensus(self):
        score, _ = project_score("anomaly", 0.1)
        assert score == _floors()["anomaly"]  # floor 60 > 0.1*100 = 10

    def test_unknown_verdict_suppresses(self):
        # An unknown verdict has no floor, so it is treated as safe (suppressed).
        score, _ = project_score("definitely-not-a-verdict", 0.9)
        assert score == 0.0

    def test_score_is_clamped_to_100(self):
        score, band = project_score("malicious", 5.0)  # 5.0*100 = 500
        assert score == 100.0
        assert band is RiskBand.CRITICAL

    def test_band_matches_score_to_band(self):
        for verdict in ("malicious", "anomaly", "benign", "normal"):
            for consensus in (None, 0.0, 0.5, 0.95):
                score, band = project_score(verdict, consensus)
                assert band.value == score_to_band(score)


def _base_result(max_score: float, max_class: str) -> ClassScoreResult:
    return ClassScoreResult(
        tx_hash="tx", network="preprod",
        scores={"phishing": max_score},
        max_score=max_score, max_class=max_class,
        risk_band=RiskBand(score_to_band(max_score)),
        sub_scores={}, evidence={},
        corroboration_count=2, corroborating_classes="phishing,circular",
    )


def _row(verdict: str = "anomaly", consensus=None, target="addr1xyz") -> dict:
    """A raw sidecar verdict row (no host-scale score; the host computes it)."""
    return {
        "tx_hash": "tx", "target": target, "cluster_id": 3,
        "iso_score": 0.7, "lof_score": 0.6, "consensus": consensus, "votes": 2,
        "verdict": verdict, "model_id": "m1", "feature_set": "shape",
        "evidence": {"top": ["fees"]},
        "scored_at": datetime(2026, 6, 22, tzinfo=timezone.utc),
    }


class TestMergeAdditivity:
    def test_higher_ca_raises_max_score_and_band(self):
        r = _base_result(45.0, "phishing")   # Moderate
        _merge_contract_anomaly(r, [_row("malicious")])   # -> 80 (Critical)
        assert r.max_score == _floors()["malicious"]
        assert r.max_class == "contract_anomaly"
        assert r.risk_band is RiskBand.CRITICAL
        assert r.scores["contract_anomaly"] == _floors()["malicious"]

    def test_lower_ca_never_lowers_existing_detection(self):
        r = _base_result(72.0, "phishing")   # High
        before_score, before_class, before_band = r.max_score, r.max_class, r.risk_band
        # A positive verdict that scores BELOW the stored detection (anomaly 60
        # < phishing 72): the stored detection must win, unchanged.
        _merge_contract_anomaly(r, [_row("anomaly")])  # -> 60 (High)
        assert r.max_score == before_score
        assert r.max_class == before_class
        assert r.risk_band is before_band
        assert r.scores["phishing"] == 72.0
        # but the contract_anomaly value is still surfaced in the payload.
        assert r.scores["contract_anomaly"] == pytest.approx(_floors()["anomaly"])

    def test_multi_target_collapses_to_highest_severity(self):
        # One tx touched two watched contracts: a benign verdict for one must
        # NOT hide the malicious verdict for the other (recall-first).
        r = _base_result(45.0, "phishing")
        rows = [
            _row("benign", consensus=0.1, target="addrA"),
            _row("malicious", consensus=None, target="addrB"),
        ]
        _merge_contract_anomaly(r, rows)
        assert r.max_score == _floors()["malicious"]
        assert r.max_class == "contract_anomaly"
        assert r.evidence["contract_anomaly"]["target"] == "addrB"

    def test_stored_corroboration_count_is_never_mutated(self):
        r = _base_result(45.0, "phishing")
        _merge_contract_anomaly(r, [_row("malicious")])
        assert r.corroboration_count == 2
        assert r.corroborating_classes == "phishing,circular"
        # the separate flag carries the contract_anomaly corroboration signal.
        assert r.contract_anomaly_corroborates is True

    def test_below_threshold_does_not_corroborate(self):
        r = _base_result(45.0, "phishing")
        # benign suppresses to 0, which is below the corroboration threshold.
        _merge_contract_anomaly(r, [_row("benign", consensus=0.10)])  # -> 0
        assert r.scores["contract_anomaly"] == 0.0
        assert r.contract_anomaly_corroborates is False

    def test_scored_at_and_evidence_surfaced(self):
        r = _base_result(45.0, "phishing")
        _merge_contract_anomaly(r, [_row("malicious")])
        assert r.contract_anomaly_scored_at == datetime(2026, 6, 22, tzinfo=timezone.utc)
        assert r.evidence["contract_anomaly"]["target"] == "addr1xyz"
        assert r.evidence["contract_anomaly"]["top"] == ["fees"]
        assert r.sub_scores["contract_anomaly"]["verdict"] == "malicious"

    def test_empty_rows_is_noop(self):
        r = _base_result(45.0, "phishing")
        _merge_contract_anomaly(r, [])
        assert "contract_anomaly" not in r.scores
        assert r.max_class == "phishing"


# --- Endpoint-level gating ---------------------------------------------------

_ROW = {
    "tx_hash": "tx", "network": "preprod",
    "token_dust": -1, "large_value": -1, "large_datum": -1, "multiple_sat": -1,
    "front_running": -1, "sandwich": -1, "circular": -1, "fake_token": -1,
    "phishing": 45.0,
    "max_score": 45.0, "max_class": "phishing", "risk_band": "Moderate",
    "sub_scores": {}, "evidence": {},
    "corroboration_count": 0, "corroborating_classes": "",
    "analysis_version": "phase5",
    "analyzed_at": datetime(2026, 6, 22, tzinfo=timezone.utc),
}


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _dev_mode_auth(monkeypatch):
    from app.auth import api_key
    monkeypatch.setattr(api_key, "_dev_mode", True)


@pytest.fixture(autouse=True)
def _stub_db(monkeypatch):
    """Stub the score read + archive lookup so no ClickHouse is needed."""
    from app.db import archive_queries, clickhouse

    async def _get_score(_tx_hash):
        return dict(_ROW)

    async def _no_archive(_net, _tx):
        return None

    monkeypatch.setattr(clickhouse, "get_class_scores_async", _get_score)
    monkeypatch.setattr(archive_queries, "archive_get_async", _no_archive)


def test_merge_skipped_when_flag_off(client, monkeypatch):
    from app.config import settings
    from app.db import clustering_queries
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", False)

    called = False

    async def _should_not_run(_net, _tx):
        nonlocal called
        called = True
        return [_row("malicious")]

    monkeypatch.setattr(clustering_queries, "get_contract_anomaly_async", _should_not_run)
    r = client.get("/api/analysis/results/tx")
    assert r.status_code == 200
    body = r.json()
    assert called is False
    assert "contract_anomaly" not in body["scores"]
    assert body["max_class"] == "phishing"


def test_merge_applied_when_flag_on(client, monkeypatch):
    from app.config import settings
    from app.db import clustering_queries
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)

    async def _verdict(_net, _tx):
        return [_row("malicious")]

    monkeypatch.setattr(clustering_queries, "get_contract_anomaly_async", _verdict)
    r = client.get("/api/analysis/results/tx")
    assert r.status_code == 200
    body = r.json()
    assert body["scores"]["contract_anomaly"] == _floors()["malicious"]
    assert body["max_class"] == "contract_anomaly"
    assert body["risk_band"] == "Critical"
    assert body["contract_anomaly_corroborates"] is True


def test_merge_best_effort_when_sidecar_errors(client, monkeypatch):
    """A sidecar read failure must not fail the main fetch."""
    from app.config import settings
    from app.db import clustering_queries
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)

    async def _boom(_net, _tx):
        raise RuntimeError("sidecar db unreachable")

    monkeypatch.setattr(clustering_queries, "get_contract_anomaly_async", _boom)
    r = client.get("/api/analysis/results/tx")
    assert r.status_code == 200
    body = r.json()
    assert body["max_class"] == "phishing"  # falls back to the stored vector
    assert "contract_anomaly" not in body["scores"]


# --- List recall rescue ------------------------------------------------------
# A tx whose STORED 9-class score is below an active score/band filter but whose
# contract_anomaly verdict projects above it must still appear in the filtered
# list: the DB filter sees only the stored score, so without the rescue the
# detection is silently dropped (recall-first violation, see CLAUDE.md).

def _full_score_row(tx_hash: str, max_score: float, max_class: str = "phishing") -> dict:
    row = dict(_ROW)
    row["tx_hash"] = tx_hash
    row["phishing"] = max_score
    row["max_score"] = max_score
    row["max_class"] = max_class
    row["risk_band"] = score_to_band(max_score)
    return row


def _bind_list_stubs(monkeypatch, *, page_rows, total, flagged, by_hashes):
    """Stub the list/count/flagged/by-hash reads used by the rescue path."""
    from app.db import clickhouse, clustering_queries

    async def _list(**_kw):
        return list(page_rows)

    async def _count(**_kw):
        return total

    async def _flagged(_net, *a, **k):
        return flagged

    async def _by_hashes(_net, hashes, *a, **k):
        return [r for r in by_hashes if r["tx_hash"] in set(hashes)]

    async def _batch(_net, _hashes):
        return {}

    monkeypatch.setattr(clickhouse, "get_class_scores_list_async", _list)
    monkeypatch.setattr(clickhouse, "count_class_scores_async", _count)
    monkeypatch.setattr(clickhouse, "get_class_scores_by_hashes_async", _by_hashes)
    monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _flagged)
    monkeypatch.setattr(clustering_queries, "get_contract_anomaly_batch_async", _batch)


def test_list_rescues_high_anomaly_below_score_filter(client, monkeypatch):
    """min_score filter: a low-stored-score tx flagged malicious is re-admitted."""
    from app.config import settings
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    # Stored score 30 (Moderate) is below the min_score=70 filter, so the DB
    # page is empty; the sidecar flagged it malicious (-> 80, Critical).
    _bind_list_stubs(
        monkeypatch,
        page_rows=[],
        total=0,
        flagged={"lowtx": [_row("malicious", target="addrZ")]},
        by_hashes=[_full_score_row("lowtx", 30.0)],
    )
    r = client.get("/api/analysis/results?network=preprod&min_score=70")
    assert r.status_code == 200
    body = r.json()
    hashes = [d["tx_hash"] for d in body["data"]]
    assert "lowtx" in hashes, "flagged tx must not be hidden by the score filter"
    rescued = next(d for d in body["data"] if d["tx_hash"] == "lowtx")
    assert rescued["max_class"] == "contract_anomaly"
    assert rescued["risk_band"] == "Critical"
    assert body["total"] == 1  # DB total 0 + 1 genuinely rescued


def test_list_rescue_skips_rows_still_below_filter(client, monkeypatch):
    """A flagged-but-benign tx that stays below the filter is NOT re-admitted."""
    from app.config import settings
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    _bind_list_stubs(
        monkeypatch,
        page_rows=[],
        total=0,
        # benign suppresses to 0, well below the min_score=70 filter.
        flagged={"lowtx": [_row("benign", consensus=0.10, target="addrZ")]},
        by_hashes=[_full_score_row("lowtx", 30.0)],
    )
    r = client.get("/api/analysis/results?network=preprod&min_score=70")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == []
    assert body["total"] == 0


def test_list_rescue_inactive_when_unfiltered(client, monkeypatch):
    """Unfiltered list: the rescue is gated off. An unfiltered score-sorted page
    orders on the stored score, and force-surfacing a buried tx onto a full page 1
    would strand a real DB row off pagination, so it isn't done (the tx is
    reachable on its later page, and the band counts/timeseries are reconciled
    separately). The default view is date-sorted, where recent CA txs appear."""
    from app.config import settings
    from app.db import clustering_queries
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    flagged_called = False

    async def _flagged(_net, *a, **k):
        nonlocal flagged_called
        flagged_called = True
        return {"lowtx": [_row("malicious")]}

    _bind_list_stubs(monkeypatch, page_rows=[], total=0, flagged={}, by_hashes=[])
    monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _flagged)
    r = client.get("/api/analysis/results?network=preprod")  # no score/band filter
    assert r.status_code == 200
    assert flagged_called is False  # gated off when unfiltered


def test_list_rescue_caps_page_to_limit(client, monkeypatch):
    """Rescued rows are re-ranked and the page is capped back to `limit`, so a
    request never returns more than `limit` rows."""
    from app.config import settings
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    # DB page already full at limit=2 (both pass the filter); two more flagged txs
    # are rescued, but the response must still cap at 2.
    _bind_list_stubs(
        monkeypatch,
        page_rows=[_full_score_row("a", 95.0), _full_score_row("b", 92.0)],
        total=2,
        flagged={"c": [_row("malicious")], "d": [_row("malicious")]},
        by_hashes=[_full_score_row("c", 10.0), _full_score_row("d", 10.0)],
    )
    r = client.get("/api/analysis/results?network=preprod&min_score=70&limit=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 2          # capped to limit
    assert body["count"] == 2
    assert body["total"] == 4              # 2 stored + 2 genuinely rescued


def test_list_rescue_inactive_under_attack_class_filter(client, monkeypatch):
    """attack_class is 9-class-specific: the synthetic class can't be a max_class,
    so the rescue/surface never fires under it."""
    from app.config import settings
    from app.db import clustering_queries
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    flagged_called = False

    async def _flagged(_net, *a, **k):
        nonlocal flagged_called
        flagged_called = True
        return {"lowtx": [_row("malicious")]}

    _bind_list_stubs(
        monkeypatch, page_rows=[], total=0, flagged={}, by_hashes=[],
    )
    monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _flagged)
    r = client.get("/api/analysis/results?network=preprod&attack_class=phishing")
    assert r.status_code == 200
    assert flagged_called is False  # gated off under a 9-class filter


# --- List filter: attack_class=contract_anomaly ------------------------------
# The synthetic class has no DB column, so the SQL path can't filter it. The
# endpoint resolves flagged txs in memory and keeps the ones whose verdict
# projects ABOVE the stored 9-class max (effective max_class = contract_anomaly).

def test_list_filter_contract_anomaly_accepts_and_returns_flagged(client, monkeypatch):
    """attack_class=contract_anomaly is no longer a 400; it returns the flagged
    txs whose sidecar verdict makes contract_anomaly their effective max_class."""
    from app.config import settings
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    # Stored phishing 30 (Moderate); malicious verdict projects to Critical, so
    # contract_anomaly becomes the effective max_class.
    _bind_list_stubs(
        monkeypatch,
        page_rows=[], total=0,
        flagged={"catx": [_row("malicious", target="addrZ")]},
        by_hashes=[_full_score_row("catx", 30.0)],
    )
    r = client.get("/api/analysis/results?network=preprod&attack_class=contract_anomaly")
    assert r.status_code == 200
    body = r.json()
    assert [d["tx_hash"] for d in body["data"]] == ["catx"]
    assert body["data"][0]["max_class"] == "contract_anomaly"
    assert body["data"][0]["risk_band"] == "Critical"
    assert body["total"] == 1


def test_list_filter_contract_anomaly_excludes_stored_dominant(client, monkeypatch):
    """A flagged tx whose stored 9-class score still dominates its verdict is a
    stored-class detection, not a contract_anomaly one, so it's excluded."""
    from app.config import settings
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    # Stored phishing 95 (Critical) > anomaly verdict (60, High): max_class stays
    # phishing, so this tx does not belong to the contract_anomaly filter.
    _bind_list_stubs(
        monkeypatch,
        page_rows=[], total=0,
        flagged={"domtx": [_row("anomaly", target="addrZ")]},
        by_hashes=[_full_score_row("domtx", 95.0)],
    )
    r = client.get("/api/analysis/results?network=preprod&attack_class=contract_anomaly")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == []
    assert body["total"] == 0


def test_list_filter_contract_anomaly_applies_band_filter(client, monkeypatch):
    """The score/band filter narrows the contract_anomaly list exactly as it does
    the stored-class list: a risk_band=Critical filter drops a High-only verdict."""
    from app.config import settings
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    _bind_list_stubs(
        monkeypatch,
        page_rows=[], total=0,
        flagged={
            "crit": [_row("malicious", target="a")],  # -> Critical
            "high": [_row("anomaly", target="b")],    # -> High
        },
        by_hashes=[_full_score_row("crit", 10.0), _full_score_row("high", 10.0)],
    )
    r = client.get(
        "/api/analysis/results?network=preprod"
        "&attack_class=contract_anomaly&risk_band=Critical"
    )
    assert r.status_code == 200
    body = r.json()
    assert [d["tx_hash"] for d in body["data"]] == ["crit"]
    assert body["total"] == 1


def test_list_filter_contract_anomaly_empty_when_clustering_disabled(client, monkeypatch):
    """With clustering off the synthetic class never exists, so the filtered page
    is legitimately empty (not a 400) and the sidecar is never queried."""
    from app.config import settings
    from app.db import clustering_queries
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", False)
    flagged_called = False

    async def _flagged(_net, *a, **k):
        nonlocal flagged_called
        flagged_called = True
        return {"catx": [_row("malicious")]}

    monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _flagged)
    r = client.get("/api/analysis/results?network=preprod&attack_class=contract_anomaly")
    assert r.status_code == 200
    body = r.json()
    assert body == {"count": 0, "total": 0, "data": []}
    assert flagged_called is False


def test_list_filter_contract_anomaly_paginates(client, monkeypatch):
    """offset/limit page the in-memory match set, and total reports the full
    count so the UI pager is consistent with the stored-class views."""
    from app.config import settings
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    _bind_list_stubs(
        monkeypatch,
        page_rows=[], total=0,
        flagged={t: [_row("malicious", target=t)] for t in ("t1", "t2", "t3")},
        by_hashes=[_full_score_row(t, 10.0) for t in ("t1", "t2", "t3")],
    )
    r = client.get(
        "/api/analysis/results?network=preprod"
        "&attack_class=contract_anomaly&limit=2&offset=0"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2          # page capped to limit
    assert body["total"] == 3          # full match count
    assert len(body["data"]) == 2


def test_list_filter_contract_anomaly_date_sort_orders_newest_first(client, monkeypatch):
    """sort=date orders the contract_anomaly list newest-first, matching the
    SQL ORDER BY the stored-class list uses (shared _sort_results helper)."""
    from app.config import settings
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    older = _full_score_row("older", 10.0)
    older["analyzed_at"] = datetime(2026, 6, 20, tzinfo=timezone.utc)
    newer = _full_score_row("newer", 10.0)
    newer["analyzed_at"] = datetime(2026, 6, 23, tzinfo=timezone.utc)
    _bind_list_stubs(
        monkeypatch,
        page_rows=[], total=0,
        flagged={
            "older": [_row("malicious", target="a")],
            "newer": [_row("malicious", target="b")],
        },
        by_hashes=[older, newer],
    )
    r = client.get(
        "/api/analysis/results?network=preprod&attack_class=contract_anomaly&sort=date"
    )
    assert r.status_code == 200
    body = r.json()
    assert [d["tx_hash"] for d in body["data"]] == ["newer", "older"]
    assert body["total"] == 2


def test_list_filter_rejects_unknown_attack_class(client, monkeypatch):
    """Validation still rejects a genuinely unknown class with a 400."""
    from app.config import settings
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    _bind_list_stubs(monkeypatch, page_rows=[], total=0, flagged={}, by_hashes=[])
    r = client.get("/api/analysis/results?network=preprod&attack_class=not_a_class")
    assert r.status_code == 400


# --- Stats / timeseries contract_anomaly augmentation ------------------------

def test_stats_reclassifies_flagged_tx_to_effective_band(client, monkeypatch):
    """A tx stored Moderate but flagged malicious (Critical) moves from the
    moderate count to the critical count, so the KPI cards don't undercount."""
    from app.config import settings
    from app.db import clickhouse, clustering_queries
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    base = {
        "total": 1, "critical_count": 0, "high_count": 0, "moderate_count": 1,
        "informational_count": 0, "avg_max_score": 45.0,
        "last_analyzed_at": None, "per_class": {}, "pending_count": 0,
    }

    async def _stats(_net, *a, **k):
        return dict(base)

    async def _flagged(_net, *a, **k):
        return {"lowtx": [_row("malicious")]}

    async def _by_hashes(_net, _hashes, *a, **k):
        return [_full_score_row("lowtx", 45.0)]  # stored Moderate

    monkeypatch.setattr(clickhouse, "get_class_scores_stats_async", _stats)
    monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _flagged)
    monkeypatch.setattr(clickhouse, "get_class_scores_by_hashes_async", _by_hashes)
    r = client.get("/api/analysis/stats?network=preprod")
    assert r.status_code == 200
    body = r.json()
    assert body["critical_count"] == 1
    assert body["moderate_count"] == 0
    # Avg Risk lifts by the per-tx delta (malicious floor 80 - stored 45) / total 1.
    assert body["avg_max_score"] == pytest.approx(80.0)


def test_timeseries_adds_contract_anomaly_only_alerts(client, monkeypatch):
    """A tx that's High/Critical only by its contract_anomaly verdict is added to
    the daily alert count, bucketed on its block date."""
    from app.config import settings
    from app.db import clickhouse, clustering_queries
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    base = [
        {"date": "2026-06-22", "count": 0},
        {"date": "2026-06-23", "count": 1},
    ]

    async def _ts(_net, days, *a, **k):
        return [dict(d) for d in base]

    async def _flagged(_net, *a, **k):
        return {"catx": [_row("malicious")]}  # -> Critical (alert)

    async def _by_hashes(_net, _hashes, *a, **k):
        return [_full_score_row("catx", 30.0)]  # stored Moderate (not an alert)

    async def _dates(_net, _hashes, _days):
        return {"catx": "2026-06-22"}

    monkeypatch.setattr(clickhouse, "get_alert_timeseries_async", _ts)
    monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _flagged)
    monkeypatch.setattr(clickhouse, "get_class_scores_by_hashes_async", _by_hashes)
    monkeypatch.setattr(clickhouse, "get_tx_block_dates_async", _dates)
    r = client.get("/api/analysis/stats/timeseries?network=preprod&days=14")
    assert r.status_code == 200
    by_date = {d["date"]: d["count"] for d in r.json()["data"]}
    assert by_date["2026-06-22"] == 1  # was 0, +1 from the contract_anomaly alert
    assert by_date["2026-06-23"] == 1  # unchanged
