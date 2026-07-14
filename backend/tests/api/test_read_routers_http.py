"""HTTP tests for the API-key-gated read/write routers that previously had
no response-shape coverage: entities GET/PUT, lifecycle reads, and the
analysis results/stats endpoints.

DB access is faked at each router module's postgres/clickhouse seam,
following the test_archive.py pattern; auth uses the dev-mode fixture.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import app.api.analysis as analysis_api
import app.api.entities as entities_api
import app.api.lifecycle as lifecycle_api
from app.analysis.engine import _CLASS_NAMES

VALID_HASH = "ab" * 32


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def auth_open(monkeypatch):
    from app.auth import api_key

    monkeypatch.setattr(api_key, "_valid_keys", [])
    monkeypatch.setattr(api_key, "_dev_mode", True)


class TestEntitiesGet:
    def test_found_shape(self, client, auth_open, monkeypatch):
        monkeypatch.setattr(
            entities_api.postgres,
            "get_entity_state",
            AsyncMock(return_value={"flagged": True}),
        )

        r = client.get("/api/entities/wallet/addr_test1qq")

        assert r.status_code == 200, r.text
        assert r.json() == {
            "entity_type": "wallet",
            "entity_id": "addr_test1qq",
            "state": {"flagged": True},
        }

    def test_missing_404(self, client, auth_open, monkeypatch):
        monkeypatch.setattr(
            entities_api.postgres,
            "get_entity_state",
            AsyncMock(return_value=None),
        )
        assert client.get("/api/entities/wallet/unknown").status_code == 404

    @pytest.mark.parametrize(
        "entity_type,entity_id",
        [("Wallet", "ok"), ("w@llet", "ok"), ("wallet", "bad id with spaces")],
        ids=["uppercase-type", "symbol-type", "spaced-id"],
    )
    def test_invalid_identifiers_rejected(self, client, auth_open, entity_type, entity_id):
        r = client.get(f"/api/entities/{entity_type}/{entity_id}")
        assert r.status_code == 400


class TestEntitiesPut:
    @pytest.fixture
    def seams(self, monkeypatch):
        setter = AsyncMock()
        auditor = AsyncMock()
        monkeypatch.setattr(entities_api.postgres, "set_entity_state", setter)
        monkeypatch.setattr(entities_api.audit, "record", auditor)
        return setter, auditor

    def test_update_persists_and_audits(self, client, auth_open, seams):
        setter, auditor = seams

        r = client.put("/api/entities/wallet/addr_test1qq", json={"flagged": True})

        assert r.status_code == 200, r.text
        assert r.json()["message"] == "Entity state updated"
        assert setter.await_args.args[2] == {"flagged": True}
        auditor.assert_awaited_once()

    def test_oversized_state_rejected(self, client, auth_open, seams):
        setter, _ = seams
        # _MAX_STATE_BYTES caps the serialized payload; one long string
        # is the simplest way over it.
        r = client.put(
            "/api/entities/wallet/addr_test1qq",
            json={"blob": "x" * (entities_api._MAX_STATE_BYTES + 1)},
        )
        assert r.status_code == 413
        setter.assert_not_awaited()

    def test_invalid_identifiers_rejected(self, client, auth_open, seams):
        setter, _ = seams
        r = client.put("/api/entities/WALLET/ok", json={})
        assert r.status_code == 400
        setter.assert_not_awaited()


class TestLifecycle:
    def test_stats_summary_shape(self, client, auth_open, monkeypatch):
        monkeypatch.setattr(
            lifecycle_api.postgres,
            "get_lifecycle_summary",
            AsyncMock(
                return_value={
                    "total_tracked": 10,
                    "pending_count": 2,
                    "confirmed_count": 7,
                    "rolled_back_count": 1,
                    "dropped_count": 0,
                    "avg_latency_ms": 1500.0,
                    "rollback_rate": 0.1,
                }
            ),
        )

        r = client.get("/api/lifecycle/stats/summary")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_tracked"] == 10
        assert body["confirmed_count"] == 7
        assert body["rollback_rate"] == 0.1

    def test_get_by_tx_id(self, client, auth_open, monkeypatch):
        monkeypatch.setattr(
            lifecycle_api.postgres,
            "get_lifecycle_by_tx_id",
            AsyncMock(
                return_value={
                    "tx_id": VALID_HASH,
                    "status": "CONFIRMED",
                    "network": "preprod",
                }
            ),
        )

        r = client.get(f"/api/lifecycle/{VALID_HASH}")

        assert r.status_code == 200, r.text
        assert r.json()["tx_id"] == VALID_HASH
        assert r.json()["status"] == "CONFIRMED"

    def test_get_missing_404(self, client, auth_open, monkeypatch):
        monkeypatch.setattr(
            lifecycle_api.postgres,
            "get_lifecycle_by_tx_id",
            AsyncMock(return_value=None),
        )
        assert client.get(f"/api/lifecycle/{VALID_HASH}").status_code == 404

    def test_list_with_status_filter(self, client, auth_open, monkeypatch):
        by_status = AsyncMock(return_value=[{"tx_id": VALID_HASH, "status": "PENDING"}])
        all_rows = AsyncMock(return_value=[])
        monkeypatch.setattr(lifecycle_api.postgres, "get_lifecycles_by_status", by_status)
        monkeypatch.setattr(lifecycle_api.postgres, "get_all_lifecycles", all_rows)

        r = client.get("/api/lifecycle?status=PENDING")

        assert r.status_code == 200, r.text
        assert r.json()["count"] == 1
        by_status.assert_awaited_once()
        all_rows.assert_not_awaited()

    def test_list_without_filter_uses_all(self, client, auth_open, monkeypatch):
        all_rows = AsyncMock(return_value=[])
        monkeypatch.setattr(lifecycle_api.postgres, "get_all_lifecycles", all_rows)

        r = client.get("/api/lifecycle")

        assert r.status_code == 200
        assert r.json() == {"count": 0, "data": []}
        all_rows.assert_awaited_once()

    def test_invalid_status_rejected(self, client, auth_open):
        assert client.get("/api/lifecycle?status=EXPLODED").status_code == 422


def _score_db_row(tx_hash=VALID_HASH, max_class="token_dust", max_score=72.0):
    row = {name: -1 for name in _CLASS_NAMES}
    row.update(
        {
            "tx_hash": tx_hash,
            "network": "preprod",
            max_class: max_score,
            "max_score": max_score,
            "max_class": max_class,
            "risk_band": "High",
            "sub_scores": {max_class: {"asset_count": 4.0}},
            "evidence": {max_class: {"reasons": ["test"]}},
            "corroboration_count": 1,
            "corroborating_classes": max_class,
            "analysis_version": "test",
            "analyzed_at": datetime.now(UTC),
        }
    )
    return row


class TestAnalysisResults:
    @pytest.fixture(autouse=True)
    def clustering_off(self, monkeypatch):
        # The overlay/rescue merges are best-effort reads against the
        # clustering store; on a dev machine with the sidecar enabled and
        # a live ClickHouse they would pollute the page with real rows.
        monkeypatch.setattr(analysis_api.settings, "CLUSTERING_ENABLED", False)

    def test_single_result_shape(self, client, auth_open, monkeypatch):
        monkeypatch.setattr(
            analysis_api.clickhouse,
            "get_class_scores_async",
            AsyncMock(return_value=_score_db_row()),
        )
        # Archive enrichment is best-effort; pin it to "not archived".
        monkeypatch.setattr(
            analysis_api.archive_queries,
            "archive_get_async",
            AsyncMock(return_value=None),
        )

        r = client.get(f"/api/analysis/results/{VALID_HASH}")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tx_hash"] == VALID_HASH
        assert body["max_class"] == "token_dust"
        assert body["risk_band"] == "High"
        # Every attack class must be present in the score vector; -1 is
        # the documented "scorer produced no finding" sentinel.
        assert set(body["scores"]) == set(_CLASS_NAMES)
        assert body["scores"]["token_dust"] == 72.0
        assert body["archived"] is None

    def test_missing_result_404(self, client, auth_open, monkeypatch):
        monkeypatch.setattr(
            analysis_api.clickhouse,
            "get_class_scores_async",
            AsyncMock(return_value=None),
        )
        r = client.get(f"/api/analysis/results/{VALID_HASH}")
        assert r.status_code == 404

    def test_list_shape(self, client, auth_open, monkeypatch):
        monkeypatch.setattr(
            analysis_api.clickhouse,
            "get_class_scores_list_async",
            AsyncMock(return_value=[_score_db_row()]),
        )
        monkeypatch.setattr(
            analysis_api.clickhouse,
            "count_class_scores_async",
            AsyncMock(return_value=41),
        )

        r = client.get("/api/analysis/results?risk_band=High&min_score=50")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 1
        assert body["total"] == 41
        assert body["data"][0]["tx_hash"] == VALID_HASH

    def test_unknown_attack_class_rejected(self, client, auth_open):
        r = client.get("/api/analysis/results?attack_class=nonsense")
        assert r.status_code == 400

    def test_unknown_sort_rejected(self, client, auth_open):
        r = client.get("/api/analysis/results?sort=alphabetical")
        assert r.status_code == 400

    def test_stats_passthrough(self, client, auth_open, monkeypatch):
        monkeypatch.setattr(
            analysis_api.clickhouse,
            "get_class_scores_stats_async",
            AsyncMock(return_value={"total_analyzed": 3, "per_class": {}}),
        )

        r = client.get("/api/analysis/stats")

        assert r.status_code == 200, r.text
        assert r.json()["total_analyzed"] == 3
