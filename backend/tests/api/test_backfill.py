"""Tests for the operator backfill endpoints.

``backfill_address`` is patched out so these exercise only the HTTP surface:
validation, the auth gate, the Kupo-not-configured guard, the already-running and
concurrency guards, and the status view. The ``_run`` outcome mapping (done /
failed / timed-out / safe-error) is tested directly against the async helper, and
the background-task wiring is driven at the function level so completion is
deterministic rather than racing the event loop.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.api.backfill as backfill_api
from app import audit
from app.api.backfill import _Job, _run
from app.ingestion.address_backfill import BackfillResult
from app.ingestion.kupo_client import KupoError, KupoUnavailable


async def _noop_audit(*_a, **_k) -> None:
    """Neutralise the audit write (no Postgres in these tests)."""
    return None


@pytest.fixture
def client(auth_open, monkeypatch):
    """Dev-mode API client (builds on the shared ``auth_open`` fixture) with Kupo
    configured and a clean job store."""
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "KUPO_URL", "http://kupo.test:1442")
    monkeypatch.setattr(settings, "CARDANO_NETWORK", "preprod")
    monkeypatch.setattr(audit, "record", _noop_audit)
    backfill_api._jobs.clear()
    return TestClient(app)


def _closed_auth_client(monkeypatch, *, current_user):
    """A client with API keys required (no dev mode) and a scripted session
    resolver, so the endpoint's real auth gate runs."""
    from app.auth import api_key as ak
    from app.auth import deps
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(ak, "_dev_mode", False)
    monkeypatch.setattr(ak, "is_valid_api_key", lambda _k: False)
    monkeypatch.setattr(deps, "current_user", current_user)
    monkeypatch.setattr(settings, "KUPO_URL", "http://kupo.test:1442")
    monkeypatch.setattr(settings, "CARDANO_NETWORK", "preprod")
    monkeypatch.setattr(audit, "record", _noop_audit)
    backfill_api._jobs.clear()
    return TestClient(app)


# A full, valid preprod (testnet) address; the network guard bech32-decodes it,
# so a truncated stub would classify as unknown and skip the guard.
_ADDR = "addr_test1qz3ql06nvc602eem2af4aefp7w5ce4ja7nuuarzavnkd06ljl64qlwnlynjwzevdrufxslpe29y47u5wxmv6nad026lqvehpe5"


async def _fast(address, *, network, max_txs, progress):
    """A backfill that completes immediately (no Ogmios/Kupo/DB)."""
    return BackfillResult(
        address, requested_txs=0, blocks_scanned=0, txs_ingested=0, missing_tx_hashes=[]
    )


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


async def test_background_task_runs_to_completion(monkeypatch):
    """Drive the create_task → _run → done wiring deterministically (no event-loop
    race) by awaiting the job's task on the test's own loop."""
    from app.api.backfill import BackfillRequest, start_backfill
    from app.config import settings

    monkeypatch.setattr(settings, "KUPO_URL", "http://kupo.test:1442")
    monkeypatch.setattr(settings, "CARDANO_NETWORK", "preprod")
    monkeypatch.setattr(backfill_api, "backfill_address", _fast)
    monkeypatch.setattr(audit, "record", _noop_audit)
    backfill_api._jobs.clear()

    class _Req:
        headers: dict = {}
        client = None

    resp = await start_backfill(
        BackfillRequest(address=_ADDR, max_txs=10), _Req(), principal="dev-mode"
    )
    assert resp["status"] == "running"
    job = backfill_api._jobs[("preprod", _ADDR)]
    await job.task
    assert job.status == "done"
    assert job.result["complete"] is True
    assert job.result["degraded_reason"] is None


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


def test_trailing_newline_address_rejected(client):
    # ADDRESS_RE is anchored with \\Z, so a value with a trailing newline (which a
    # bare $ would accept, matching just before the final \\n) is rejected.
    resp = client.post("/api/v1/backfill", json={"address": _ADDR + "\n"})
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


def test_429_when_at_concurrency_limit(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "BACKFILL_MAX_CONCURRENT", 1)
    # A DIFFERENT address is already running, so this trips the global cap (429),
    # not the same-address guard (409).
    backfill_api._jobs[("preprod", "addr_test1qother")] = _Job(
        status="running",
        address="addr_test1qother",
        network="preprod",
        max_txs=100,
        started_at="2026-07-15T00:00:00+00:00",
    )
    resp = client.post("/api/v1/backfill", json={"address": _ADDR})
    assert resp.status_code == 429


def test_status_404_for_unknown_address(client):
    resp = client.get("/api/v1/backfill/addr_test1qunknownxxxxxxxxxx")
    assert resp.status_code == 404


def test_post_requires_auth_401(monkeypatch):
    async def _no_session(_request):
        return None

    client = _closed_auth_client(monkeypatch, current_user=_no_session)
    resp = client.post("/api/v1/backfill", json={"address": _ADDR})
    assert resp.status_code == 401


def test_post_rejects_non_admin_session_403(monkeypatch):
    async def _reviewer(_request):
        return {"id": 7, "role": "Reviewer"}

    client = _closed_auth_client(monkeypatch, current_user=_reviewer)
    resp = client.post("/api/v1/backfill", json={"address": _ADDR})
    assert resp.status_code == 403


def test_status_read_open_to_any_authenticated(client):
    # The GET status route stays on verify_api_key (a plain authenticated read),
    # so dev-mode reaches it and returns 404 for an unknown address, not 401.
    resp = client.get(f"/api/v1/backfill/{_ADDR}")
    assert resp.status_code == 404


def test_post_writes_audit_row(client, monkeypatch):
    calls: list[dict] = []

    async def _spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(audit, "record", _spy)
    monkeypatch.setattr(backfill_api, "backfill_address", _fast)

    resp = client.post("/api/v1/backfill", json={"address": _ADDR, "max_txs": 12})
    assert resp.status_code == 202
    assert len(calls) == 1
    row = calls[0]
    assert row["event_type"] == "address_backfill"
    assert row["action"] == "start"
    assert row["entity_id"] == _ADDR
    assert row["details"]["max_txs"] == 12


async def test_run_maps_result_to_done() -> None:
    job = _Job("running", _ADDR, "preprod", 100, "2026-07-15T00:00:00+00:00")

    async def _fake(address, *, network, max_txs, progress):
        progress("working")
        return BackfillResult(
            address, requested_txs=3, blocks_scanned=5, txs_ingested=2, missing_tx_hashes=["cc"]
        )

    orig = backfill_api.backfill_address
    backfill_api.backfill_address = _fake  # type: ignore[assignment]
    try:
        await _run(job)
    finally:
        backfill_api.backfill_address = orig  # type: ignore[assignment]

    assert job.status == "done"
    assert job.result == {
        "requested_txs": 3,
        "txs_ingested": 2,
        "blocks_scanned": 5,
        "missing_tx_hashes": ["cc"],
        "complete": True,
        "degraded_reason": None,
    }


async def test_run_surfaces_degraded_flags() -> None:
    job = _Job("running", _ADDR, "preprod", 100, "2026-07-15T00:00:00+00:00")

    async def _degraded(address, *, network, max_txs, progress):
        return BackfillResult(
            address,
            requested_txs=2,
            blocks_scanned=2,
            txs_ingested=2,
            missing_tx_hashes=[],
            complete=False,
            degraded_reason="Kupo indexed only to slot 100",
        )

    orig = backfill_api.backfill_address
    backfill_api.backfill_address = _degraded  # type: ignore[assignment]
    try:
        await _run(job)
    finally:
        backfill_api.backfill_address = orig  # type: ignore[assignment]

    assert job.status == "done"
    assert job.result["complete"] is False
    assert "slot 100" in job.result["degraded_reason"]


async def test_run_records_failure_safely() -> None:
    job = _Job("running", _ADDR, "preprod", 100, "2026-07-15T00:00:00+00:00")

    async def _boom(*_a, **_k):
        # A raw message carrying the internal Kupo URL must not reach the client.
        raise KupoError("Kupo request to http://kupo.internal:1442/matches failed: 500 boom")

    orig = backfill_api.backfill_address
    backfill_api.backfill_address = _boom  # type: ignore[assignment]
    try:
        await _run(job)
    finally:
        backfill_api.backfill_address = orig  # type: ignore[assignment]

    assert job.status == "failed"
    assert "kupo.internal" not in (job.error or "")
    assert "server logs" in (job.error or "")


async def test_run_times_out(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "BACKFILL_TIMEOUT_SECONDS", 0.01)

    async def _hang(*_a, **_k):
        await asyncio.sleep(10)

    monkeypatch.setattr(backfill_api, "backfill_address", _hang)
    job = _Job("running", _ADDR, "preprod", 100, "2026-07-15T00:00:00+00:00")
    await _run(job)
    assert job.status == "failed"
    assert "time limit" in (job.error or "")


async def test_kupo_unavailable_maps_to_safe_error() -> None:
    job = _Job("running", _ADDR, "preprod", 100, "2026-07-15T00:00:00+00:00")

    async def _unavail(*_a, **_k):
        raise KupoUnavailable("KUPO_URL is not configured")

    orig = backfill_api.backfill_address
    backfill_api.backfill_address = _unavail  # type: ignore[assignment]
    try:
        await _run(job)
    finally:
        backfill_api.backfill_address = orig  # type: ignore[assignment]

    assert job.status == "failed"
    assert "Kupo is not configured" in (job.error or "")


def test_evicts_finished_jobs_over_retention(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "BACKFILL_JOB_RETENTION", 2)
    backfill_api._jobs.clear()
    # Three finished jobs (distinct started_at) + one running.
    for i in range(3):
        backfill_api._jobs[("preprod", f"a{i}")] = _Job(
            status="done",
            address=f"a{i}",
            network="preprod",
            max_txs=1,
            started_at=f"2026-07-15T00:0{i}:00+00:00",
        )
    backfill_api._jobs[("preprod", "running")] = _Job(
        status="running",
        address="running",
        network="preprod",
        max_txs=1,
        started_at="2026-07-15T00:09:00+00:00",
    )
    backfill_api._evict_finished_jobs()
    finished = {k for k, j in backfill_api._jobs.items() if j.status == "done"}
    assert ("preprod", "a0") not in finished  # oldest finished evicted
    assert len(finished) == 2
    assert ("preprod", "running") in backfill_api._jobs  # never evicted
