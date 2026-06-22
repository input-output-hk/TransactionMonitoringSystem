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

    def test_consensus_can_raise_a_no_floor_verdict(self):
        # normal carries no floor, so a high consensus drives the score.
        scale = scorer_config.contract_anomaly_config()["consensus_scale"]
        score, _ = project_score("normal", 0.9)
        assert score == pytest.approx(0.9 * scale)

    def test_floor_wins_over_low_consensus(self):
        score, _ = project_score("anomaly", 0.1)
        assert score == _floors()["anomaly"]  # floor 60 > 0.1*100 = 10

    def test_unknown_verdict_defaults_to_normal_floor(self):
        score, _ = project_score("definitely-not-a-verdict", None)
        assert score == _floors()["normal"]

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
        _merge_contract_anomaly(r, [_row("normal", consensus=0.30)])  # -> 30
        # max_score/class/band unchanged; existing phishing score untouched.
        assert r.max_score == before_score
        assert r.max_class == before_class
        assert r.risk_band is before_band
        assert r.scores["phishing"] == 72.0
        # but the contract_anomaly value is still surfaced in the payload.
        assert r.scores["contract_anomaly"] == pytest.approx(30.0)

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
        _merge_contract_anomaly(r, [_row("benign", consensus=0.10)])  # -> 10
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
