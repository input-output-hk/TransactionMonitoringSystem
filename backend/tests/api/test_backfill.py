"""Tests for the operator backfill endpoints.

``backfill_address`` is patched out so these exercise only the HTTP surface:
validation, the Kupo-not-configured guard, the already-running guard, and the
status view. The ``_run`` outcome mapping (done/failed) is tested directly
against the async helper without the event-loop timing of a background task.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.backfill as backfill_api
from app.api.backfill import _Job, _run
from app.ingestion.address_backfill import BackfillResult
from app.ingestion.kupo_client import KupoUnavailable


@pytest.fixture
def client(monkeypatch):
    from app.auth import api_key
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(api_key, "_valid_keys", [])
    monkeypatch.setattr(api_key, "_dev_mode", True)
    monkeypatch.setattr(settings, "KUPO_URL", "http://kupo.test:1442")
    monkeypatch.setattr(settings, "CARDANO_NETWORK", "preprod")
    backfill_api._jobs.clear()
    return TestClient(app)


_ADDR = "addr_test1qz3ql06nvc602eem2af4aefp7w5ce4ja7nuuarzavnkd06l"


async def _fast(address, *, network, max_txs, progress):
    """A backfill that completes immediately (no Ogmios/Kupo/DB)."""
    return BackfillResult(address, requested_txs=0, blocks_scanned=0, txs_ingested=0, missing_tx_hashes=[])


def test_post_starts_job_and_status_reports_it(client, monkeypatch):
    monkeypatch.setattr(backfill_api, "backfill_address", _fast)

    resp = client.post("/api/v1/backfill", json={"address": _ADDR, "max_txs": 200})
    assert resp.status_code == 202
    body = resp.json()
    assert body["address"] == _ADDR and body["network"] == "preprod"
    assert body["max_txs"] == 200
    # The response is built at creation time, before the task's first await.
    assert body["status"] == "running"

    status = client.get(f"/api/v1/backfill/{_ADDR}")
    assert status.status_code == 200
    # The background task may or may not have finished by now; both are valid.
    assert status.json()["status"] in {"running", "done"}


def test_max_txs_defaulted_and_capped(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "BACKFILL_DEFAULT_MAX_TXS", 500)
    monkeypatch.setattr(settings, "BACKFILL_MAX_TXS_CAP", 1000)
    monkeypatch.setattr(backfill_api, "backfill_address", _fast)

    # Omitted → default.
    r1 = client.post("/api/v1/backfill", json={"address": _ADDR})
    assert r1.json()["max_txs"] == 500
    backfill_api._jobs.clear()
    # Above the cap → clamped.
    r2 = client.post("/api/v1/backfill", json={"address": _ADDR, "max_txs": 99999})
    assert r2.json()["max_txs"] == 1000


def test_invalid_address_rejected(client):
    resp = client.post("/api/v1/backfill", json={"address": "bad addr!!"})
    assert resp.status_code == 422


# A real mainnet Shelley address (the Djed v1 script from this investigation).
_MAINNET_ADDR = "addr1wxy49hzx86ch868hr3uz98lqw8p7ef55j6x8ras7udy3a0gm8cdla"


def test_mainnet_address_rejected_on_testnet_backend(client):
    # Backend is preprod (fixture); a mainnet address can never match → 422.
    resp = client.post("/api/v1/backfill", json={"address": _MAINNET_ADDR})
    assert resp.status_code == 422
    assert "mainnet" in resp.json()["detail"]


def test_testnet_address_rejected_on_mainnet_backend(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "CARDANO_NETWORK", "mainnet")
    resp = client.post("/api/v1/backfill", json={"address": _ADDR})  # addr_test1...
    assert resp.status_code == 422
    assert "testnet" in resp.json()["detail"]


def test_503_when_kupo_not_configured(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "KUPO_URL", "")
    resp = client.post("/api/v1/backfill", json={"address": _ADDR})
    assert resp.status_code == 503


def test_409_when_already_running(client):
    backfill_api._jobs[("preprod", _ADDR)] = _Job(
        status="running",
        address=_ADDR,
        network="preprod",
        max_txs=100,
        started_at="2026-07-15T00:00:00+00:00",
    )
    resp = client.post("/api/v1/backfill", json={"address": _ADDR})
    assert resp.status_code == 409


def test_status_404_for_unknown_address(client):
    resp = client.get("/api/v1/backfill/addr_test1qunknownxxxxxxxxxx")
    assert resp.status_code == 404


async def test_run_maps_result_to_done() -> None:
    job = _Job("running", _ADDR, "preprod", 100, "2026-07-15T00:00:00+00:00")

    async def _fake(address, *, network, max_txs, progress):
        progress("working")
        return BackfillResult(address, requested_txs=3, blocks_scanned=5, txs_ingested=2, missing_tx_hashes=["cc"])

    import app.api.backfill as mod

    orig = mod.backfill_address
    mod.backfill_address = _fake  # type: ignore[assignment]
    try:
        await _run(job)
    finally:
        mod.backfill_address = orig  # type: ignore[assignment]

    assert job.status == "done"
    assert job.result == {
        "requested_txs": 3,
        "txs_ingested": 2,
        "blocks_scanned": 5,
        "missing_tx_hashes": ["cc"],
    }


async def test_run_records_failure() -> None:
    job = _Job("running", _ADDR, "preprod", 100, "2026-07-15T00:00:00+00:00")

    async def _boom(*_a, **_k):
        raise KupoUnavailable("KUPO_URL is not configured")

    import app.api.backfill as mod

    orig = mod.backfill_address
    mod.backfill_address = _boom  # type: ignore[assignment]
    try:
        await _run(job)
    finally:
        mod.backfill_address = orig  # type: ignore[assignment]

    assert job.status == "failed"
    assert "KUPO_URL" in (job.error or "")
