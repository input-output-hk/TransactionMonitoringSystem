"""FastAPI endpoint tests via TestClient with the repo dependency overridden.

The app's lifespan (real JobManager + ClickHouse) is NOT triggered — TestClient
is used without its context manager and ``app.state.job_manager`` is stubbed — so
these exercise routing, validation, error mapping, auth and the enqueue guards
with no network or database.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import app, get_request_repo
from app.config import Settings
from tests.fakes import FakeRepoBase


class FakeManager:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        # Mirrors JobManager.enqueue_lock: the endpoints guard the
        # check-create-enqueue sequence with `with manager.enqueue_lock`.
        self.enqueue_lock = threading.Lock()

    def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)


class FakeApiRepo(FakeRepoBase):
    def __init__(self, **data: Any) -> None:
        self.contracts: list[dict[str, Any]] = data.get("contracts", [])
        self.jobs: list[dict[str, Any]] = data.get("jobs", [])
        self.runs: dict[str, dict[str, Any]] = data.get("runs", {})
        self._nonterminal: list[dict[str, Any]] = data.get("nonterminal", [])
        self.ping_ok: bool = data.get("ping_ok", True)
        self.saved_contracts: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.created_jobs: list[tuple[Any, ...]] = []
        self.label_calls: list[tuple[str, list[str], str]] = []
        self.clear_calls: list[tuple[str, list[str]]] = []
        self.deleted_anomaly_runs: list[str] = []

    def ping(self) -> bool:
        if not self.ping_ok:
            raise RuntimeError("down")
        return True

    def list_contracts(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self.contracts[offset : offset + limit]

    def count_contracts(self) -> int:
        return len(self.contracts)

    def get_contract(self, target: str) -> dict[str, Any] | None:
        return next((c for c in self.contracts if c["target"] == target), None)

    def list_jobs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self.jobs[offset : offset + limit]

    def count_jobs(self) -> int:
        return len(self.jobs)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return next((j for j in self.jobs if j["job_id"] == job_id), None)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.get(run_id)

    def list_targets(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return []

    def count_targets(self) -> int:
        return 0

    def nonterminal_jobs(self) -> list[dict[str, Any]]:
        return self._nonterminal

    def save_contract(self, contract: dict[str, Any]) -> None:
        self.saved_contracts.append(contract)

    def update_contract_label(self, target: str, label: str) -> dict[str, Any] | None:
        c = self.get_contract(target)
        if c is None:
            return None
        c["label"] = label
        return c

    def delete_contract(self, target: str) -> dict[str, Any]:
        self.deleted.append(target)
        return {"target": target}

    def create_job(self, *args: Any) -> None:
        self.created_jobs.append(args)

    # --- Verdict labels (cluster/graph decoration + label endpoints) ---------
    # Canned data optional; defaults keep unrelated tests working.
    def run_tx_labels(self, run_id: str) -> dict[str, int]:
        return getattr(self, "_membership", {})

    def labels_for_target(self, target: str) -> dict[str, str]:
        return getattr(self, "_explicit", {})

    def cluster_labeled_hashes(self, target: str) -> set[str]:
        return set(getattr(self, "_explicit", {}))

    def latest_anomaly_run(
        self, target: str, feature_set: str, *, near: str | None = None
    ) -> str | None:
        return getattr(self, "_anomaly_run", None)

    def anomaly_votes_for_run(self, run_id: str) -> dict[str, int]:
        return getattr(self, "_votes", {})

    def get_anomaly_run(self, run_id: str) -> dict[str, Any] | None:
        return getattr(self, "_anomaly_runs", {}).get(run_id)

    def delete_anomaly_run(self, run_id: str) -> None:
        self.deleted_anomaly_runs.append(run_id)

    def cluster_summary(self, run_id: str, target: str) -> list[dict[str, Any]]:
        return getattr(self, "_summary", [])

    def cluster_transactions(
        self, run_id: str, target: str, cluster_id: int, **k: Any
    ) -> list[dict[str, Any]]:
        txs = getattr(self, "_txs", [])
        return txs.get(cluster_id, []) if isinstance(txs, dict) else txs

    def cluster_member_hashes(self, run_id: str, cluster_id: int) -> list[str]:
        return getattr(self, "_members", {}).get(cluster_id, [])

    def set_tx_labels(self, target: str, tx_hashes: Any, label: str, **k: Any) -> int:
        self.label_calls.append((target, list(tx_hashes), label))
        return len(list(tx_hashes))

    def clear_tx_labels(self, target: str, tx_hashes: Any) -> int:
        self.clear_calls.append((target, list(tx_hashes)))
        return len(list(tx_hashes))

    def close(self) -> None:
        pass


def _contract_row(**over: Any) -> dict[str, Any]:
    """A full contract row as the real repo returns it; override what the test cares about."""
    row: dict[str, Any] = {
        "target": "addr1a",
        "target_type": "address",
        "label": "",
        "exists": 1,
        "is_script": 1,
        "script_type": "plutusV2",
        "balance_lovelace": 0,
        "asset_count": 0,
        "sample_tokens": "[]",
        "status": "done",
        "requested_max_txs": 0,
        "updated_at": "2026-01-01 00:00:00.000000",
        "tx_count": 0,
        "drift_score": 0.0,
    }
    row.update(over)
    return row


def _job_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "job_id": "job-1",
        "target": "addr1a",
        "target_type": "address",
        "max_txs": 0,
        "reprocess": 0,
        "kind": "onboard",
        "status": "done",
        "stage_detail": "",
        "txs_done": 0,
        "error": "",
        "created_at": "2026-01-01 00:00:00.000000",
        "updated_at": "2026-01-01 00:00:00.000000",
    }
    row.update(over)
    return row


def _client(repo: FakeApiRepo, manager: FakeManager | None = None) -> TestClient:
    app.dependency_overrides[get_request_repo] = lambda: repo
    app.state.job_manager = manager or FakeManager()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_overrides() -> Any:
    yield
    app.dependency_overrides.clear()


# --- Health / readiness ----------------------------------------------------


def test_health_is_liveness_only() -> None:
    client = _client(FakeApiRepo())
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_ready_ok() -> None:
    r = _client(FakeApiRepo(ping_ok=True)).get("/api/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_ready_503_when_clickhouse_down() -> None:
    r = _client(FakeApiRepo(ping_ok=False)).get("/api/ready")
    assert r.status_code == 503
    assert "clickhouse" in r.json()["detail"]  # generic, no internal exc text


def test_config_reports_host_backed_and_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # host_ch source → host_backed True; the UI hides the per-contract max-txs cap.
    monkeypatch.setattr(
        "app.api.routers.system.get_settings",
        lambda: Settings(CHAIN_SOURCE="host_ch", CLUSTERING_WINDOW_TXS=12_345),
    )
    body = _client(FakeApiRepo()).get("/api/config").json()
    assert body == {"host_backed": True, "window_txs": 12_345}


def test_config_non_host_backed_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # A download-backed source → host_backed False; the form shows the max-txs cap.
    monkeypatch.setattr(
        "app.api.routers.system.get_settings",
        lambda: Settings(CHAIN_SOURCE="other", CLUSTERING_WINDOW_TXS=500),
    )
    body = _client(FakeApiRepo()).get("/api/config").json()
    assert body == {"host_backed": False, "window_txs": 500}


# --- Reads -----------------------------------------------------------------


def test_list_contracts() -> None:
    repo = FakeApiRepo(contracts=[_contract_row(tx_count=5)])
    r = _client(repo).get("/api/contracts")
    assert r.status_code == 200 and r.json()["data"][0]["target"] == "addr1a"


# --- List pagination envelope ------------------------------------------------


def test_list_envelope_shape_total_exceeds_count() -> None:
    """{count, total, data}: count is the page length, total the full collection
    size, so total > count signals more rows beyond this page."""
    jobs = [_job_row(job_id=f"job-{i}") for i in range(3)]
    body = _client(FakeApiRepo(jobs=jobs)).get("/api/jobs?limit=2").json()
    assert set(body) == {"count", "total", "data"}
    assert body["count"] == 2 and body["total"] == 3
    assert len(body["data"]) == 2 and body["count"] > 0 and body["total"] > body["count"]


def test_jobs_list_limit_offset_slice() -> None:
    jobs = [_job_row(job_id=f"job-{i}") for i in range(5)]
    client = _client(FakeApiRepo(jobs=jobs))
    body = client.get("/api/jobs?limit=2&offset=2").json()
    assert [j["job_id"] for j in body["data"]] == ["job-2", "job-3"]
    assert body["count"] == 2 and body["total"] == 5
    # Trailing partial page: count reflects what the page actually holds.
    body = client.get("/api/jobs?limit=2&offset=4").json()
    assert [j["job_id"] for j in body["data"]] == ["job-4"]
    assert body["count"] == 1 and body["total"] == 5


def test_jobs_list_default_envelope() -> None:
    body = _client(FakeApiRepo(jobs=[_job_row()])).get("/api/jobs").json()
    assert body["count"] == 1 and body["total"] == 1
    assert body["data"][0]["job_id"] == "job-1"


def test_list_limit_bounds_rejected() -> None:
    client = _client(FakeApiRepo())
    assert client.get("/api/jobs?limit=1001").status_code == 422  # over le=1000
    assert client.get("/api/jobs?limit=0").status_code == 422  # under ge=1
    assert client.get("/api/jobs?offset=-1").status_code == 422  # under ge=0


def test_get_contract_404() -> None:
    r = _client(FakeApiRepo()).get("/api/contracts/addr1missing")
    assert r.status_code == 404


def test_contract_reclustering_suggested_derived_from_drift_score() -> None:
    """`reclustering_suggested` is derived from drift_score vs the default 0.25
    threshold at read time (not stored)."""
    high = FakeApiRepo(contracts=[_contract_row(drift_score=0.4)])
    body = _client(high).get("/api/contracts/addr1a").json()
    assert body["drift_score"] == 0.4 and body["reclustering_suggested"] is True

    low = FakeApiRepo(contracts=[_contract_row(drift_score=0.1)])
    body = _client(low).get("/api/contracts/addr1a").json()
    assert body["reclustering_suggested"] is False


def test_delete_contract_purges() -> None:
    repo = FakeApiRepo(contracts=[_contract_row(tx_count=5)])
    r = _client(repo).delete("/api/contracts/addr1a")
    assert r.status_code == 200 and r.json() == {"deleted": True, "target": "addr1a"}
    assert repo.deleted == ["addr1a"]


def test_delete_contract_404_unknown() -> None:
    repo = FakeApiRepo()
    r = _client(repo).delete("/api/contracts/addr1missing")
    assert r.status_code == 404
    assert repo.deleted == []


def test_delete_contract_409_when_job_running() -> None:
    repo = FakeApiRepo(
        contracts=[_contract_row(status="processing")],
        nonterminal=[{"target": "addr1a", "status": "downloading"}],
    )
    r = _client(repo).delete("/api/contracts/addr1a")
    assert r.status_code == 409
    assert repo.deleted == []  # not purged while a job is mid-write


def test_get_job_found_and_404() -> None:
    repo = FakeApiRepo(jobs=[_job_row()])
    client = _client(repo)
    assert client.get("/api/jobs/job-1").json()["status"] == "done"
    assert client.get("/api/jobs/nope").status_code == 404


def test_get_run_404() -> None:
    assert _client(FakeApiRepo()).get("/api/runs/nope").status_code == 404


# --- Cluster verdict labels ------------------------------------------------


def _run_repo(**extra: Any) -> FakeApiRepo:
    repo = FakeApiRepo(
        runs={
            "r1": {
                "run_id": "r1",
                "target": "addr",
                "feature_set": "shape",
                "created_at": "2024-01-01 00:00:00.000000",
            }
        }
    )
    for k, v in extra.items():
        setattr(repo, k, v)
    return repo


def test_label_cluster_writes_labels() -> None:
    repo = _run_repo(_members={0: ["aa", "bb"]})
    r = _client(repo).post("/api/runs/r1/clusters/0/label", json={"verdict": "malicious"})
    assert r.status_code == 200
    assert r.json()["labeled"] == 2
    assert repo.label_calls == [("addr", ["aa", "bb"], "malicious")]


def test_label_cluster_404_unknown_run() -> None:
    r = _client(FakeApiRepo()).post("/api/runs/nope/clusters/0/label", json={"verdict": "benign"})
    assert r.status_code == 404


def test_label_cluster_404_empty_cluster() -> None:
    # Valid run, but cluster 9 has no members in it → not found.
    r = _client(_run_repo(_members={0: ["aa"]})).post(
        "/api/runs/r1/clusters/9/label", json={"verdict": "malicious"}
    )
    assert r.status_code == 404


def test_label_cluster_422_bad_verdict() -> None:
    r = _client(_run_repo()).post("/api/runs/r1/clusters/0/label", json={"verdict": "evil"})
    assert r.status_code == 422


def test_label_cluster_422_noise_bucket() -> None:
    r = _client(_run_repo()).post("/api/runs/r1/clusters/-1/label", json={"verdict": "malicious"})
    assert r.status_code == 422


def test_clear_cluster_label() -> None:
    repo = _run_repo(_members={0: ["aa"]})
    r = _client(repo).post("/api/runs/r1/clusters/0/clear-label")
    assert r.status_code == 200
    assert r.json()["cleared"] == 1
    assert repo.clear_calls == [("addr", ["aa"])]


def test_clear_cluster_label_422_noise_bucket() -> None:
    r = _client(_run_repo()).post("/api/runs/r1/clusters/-1/clear-label")
    assert r.status_code == 422


def test_cluster_summary_includes_verdict_fields() -> None:
    summary = [
        {
            "cluster_id": 0,
            "size": 2,
            "avg_fees": 0.0,
            "avg_output_lovelace": 0.0,
            "avg_inputs": 0.0,
            "avg_outputs": 0.0,
            "avg_assets": 0.0,
        }
    ]
    repo = _run_repo(
        _summary=summary,
        _membership={"aa": 0, "bb": 0},
        _explicit={"aa": "malicious"},
        _anomaly_run="an1",
        _votes={"bb": 3},
    )
    row = _client(repo).get("/api/runs/r1/clusters").json()[0]
    assert row["verdict"] == "malicious"  # inherited from "aa"
    assert row["labeled_count"] == 1
    assert row["anomaly_count"] == 1  # "bb" has votes >= 2


def _txrow(h: str) -> dict[str, Any]:
    return {
        "tx_hash": h,
        "block_time": "t",
        "fees": 0,
        "total_output_lovelace": 0,
        "input_count": 0,
        "output_count": 0,
        "distinct_assets": 0,
        "redeemer_count": 0,
    }


def test_cluster_transactions_include_per_tx_verdict() -> None:
    # cluster 0: aa is explicitly benign (suppresses its anomaly).
    # cluster 1: bb is unlabeled with votes >= 2 → anomaly.
    repo = _run_repo(
        _txs={0: [_txrow("aa")], 1: [_txrow("bb")]},
        _members={0: ["aa"], 1: ["bb"]},
        _explicit={"aa": "benign"},
        _anomaly_run="an1",
        _votes={"aa": 3, "bb": 3},
    )
    client = _client(repo)
    c0 = client.get("/api/runs/r1/clusters/0/transactions").json()["transactions"][0]
    assert c0["tx_hash"] == "aa" and c0["verdict"] == "benign" and c0["votes"] == 3
    c1 = client.get("/api/runs/r1/clusters/1/transactions").json()["transactions"][0]
    assert c1["tx_hash"] == "bb" and c1["verdict"] == "anomaly"


# --- POST /api/contracts ---------------------------------------------------


def test_create_contract_rejects_invalid_target() -> None:
    r = _client(FakeApiRepo()).post("/api/contracts", json={"target": "not a target!"})
    assert r.status_code == 422


def test_create_contract_enqueues_job() -> None:
    repo = FakeApiRepo()
    manager = FakeManager()
    client = _client(repo, manager)
    r = client.post("/api/contracts", json={"target": "addr1qxyztest0001", "max_txs": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["target"] == "addr1qxyztest0001" and body["target_type"] == "address"
    assert len(repo.saved_contracts) == 1
    assert len(repo.created_jobs) == 1
    assert manager.enqueued == [body["job_id"]]


def test_create_contract_persists_display_name() -> None:
    repo = FakeApiRepo()
    client = _client(repo)
    r = client.post("/api/contracts", json={"target": "addr1qxyztest0001", "label": "  My Vault  "})
    assert r.status_code == 200
    assert repo.saved_contracts[0]["label"] == "My Vault"  # stripped


def test_create_contract_keeps_existing_label_when_blank() -> None:
    # Re-adding without a name must not clobber a previously-set custom name.
    repo = FakeApiRepo(contracts=[_contract_row(target="addr1qxyztest0001", label="My Name")])
    client = _client(repo)
    r = client.post("/api/contracts", json={"target": "addr1qxyztest0001"})
    assert r.status_code == 200
    assert repo.saved_contracts[0]["label"] == "My Name"


def test_create_contract_explicit_label_overrides_existing() -> None:
    repo = FakeApiRepo(contracts=[_contract_row(target="addr1qxyztest0001", label="Old")])
    client = _client(repo)
    client.post("/api/contracts", json={"target": "addr1qxyztest0001", "label": "New"})
    assert repo.saved_contracts[0]["label"] == "New"


def test_create_contract_max_txs_cap() -> None:
    r = _client(FakeApiRepo()).post(
        "/api/contracts", json={"target": "addr1qxyztest0001", "max_txs": 10_000_000}
    )
    assert r.status_code == 422  # exceeds MAX_TXS_CAP


def test_create_contract_conflict_when_already_running() -> None:
    repo = FakeApiRepo(nonterminal=[{"target": "addr1qxyztest0001", "job_id": "j0"}])
    r = _client(repo).post("/api/contracts", json={"target": "addr1qxyztest0001"})
    assert r.status_code == 409


def test_create_contract_429_when_too_many_inflight() -> None:
    busy = [{"target": f"addr1other{i:04d}", "job_id": f"j{i}"} for i in range(8)]
    repo = FakeApiRepo(nonterminal=busy)
    r = _client(repo).post("/api/contracts", json={"target": "addr1qxyztest0001"})
    assert r.status_code == 429


# --- GET /api/registry/identify --------------------------------------------

# Minswap "Order Contract" — present in the vendored registry snapshot.
_MINSWAP_HASH = "a65ca58a4e9c755fa830173d2a5caed458ac0c73f97db7faae2e7e3b"


def test_identify_known_policy_returns_label() -> None:
    r = _client(FakeApiRepo()).get(f"/api/registry/identify?target={_MINSWAP_HASH}")
    body = r.json()
    assert r.status_code == 200 and body["valid"] is True
    assert body["target_type"] == "policy"
    assert body["script_hash"] == _MINSWAP_HASH
    assert body["label"] == "Minswap Order Contract"


def test_identify_valid_but_unknown_has_empty_label() -> None:
    body = _client(FakeApiRepo()).get(f"/api/registry/identify?target={'00' * 28}").json()
    assert body["valid"] is True and body["label"] == ""


def test_identify_invalid_target_is_not_an_error() -> None:
    body = _client(FakeApiRepo()).get("/api/registry/identify?target=not a target!").json()
    assert body["valid"] is False and body["label"] == ""


# --- PATCH /api/contracts/{target} (rename) --------------------------------


def test_rename_contract_updates_label() -> None:
    repo = FakeApiRepo(contracts=[_contract_row()])
    r = _client(repo).patch("/api/contracts/addr1a", json={"label": "My Name"})
    assert r.status_code == 200 and r.json()["label"] == "My Name"


def test_rename_contract_404_when_missing() -> None:
    r = _client(FakeApiRepo()).patch("/api/contracts/addr1missing", json={"label": "x"})
    assert r.status_code == 404


# --- DELETE /api/anomaly-runs/{run_id} -------------------------------------


def test_delete_custom_anomaly_run() -> None:
    repo = FakeApiRepo()
    repo._anomaly_runs = {"an1": {"run_id": "an1", "target": "addr", "origin": "custom"}}
    r = _client(repo).delete("/api/anomaly-runs/an1")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert repo.deleted_anomaly_runs == ["an1"]


def test_delete_system_anomaly_run_forbidden() -> None:
    repo = FakeApiRepo()
    repo._anomaly_runs = {"an1": {"run_id": "an1", "target": "addr", "origin": "system"}}
    r = _client(repo).delete("/api/anomaly-runs/an1")
    assert r.status_code == 403
    assert repo.deleted_anomaly_runs == []


def test_delete_missing_anomaly_run_404() -> None:
    repo = FakeApiRepo()
    r = _client(repo).delete("/api/anomaly-runs/nope")
    assert r.status_code == 404
    assert repo.deleted_anomaly_runs == []


# --- Auth ------------------------------------------------------------------


def test_api_key_enforced_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.api.deps.get_settings",
        lambda: Settings(API_KEY="secret"),
    )
    client = _client(FakeApiRepo(contracts=[]))
    assert client.get("/api/contracts").status_code == 401
    assert client.get("/api/contracts", headers={"X-API-Key": "secret"}).status_code == 200
    # Probe endpoints stay open even with a key configured.
    assert client.get("/api/health").status_code == 200


# --- Target normalization: policy ids are case-insensitive -------------------


def test_policy_target_case_insensitive_across_endpoints() -> None:
    """POST lowercases stored policy ids; every {target} path lookup must apply
    the same normalization or POST and GET would disagree about the same policy."""
    lower = "a1" * 28  # 56-hex, stored canonical form
    upper = lower.upper()
    repo = FakeApiRepo(contracts=[_contract_row(target=lower, target_type="policy")])
    client = _client(repo)
    # GET / PATCH / DELETE with the uppercase variant all hit the stored row.
    assert client.get(f"/api/contracts/{upper}").status_code == 200
    assert client.patch(f"/api/contracts/{upper}", json={"label": "x"}).status_code == 200
    assert client.delete(f"/api/contracts/{upper}").status_code == 200
    assert repo.deleted == [lower]  # deleted under the canonical casing


def test_create_contract_normalizes_policy_case(monkeypatch: pytest.MonkeyPatch) -> None:
    # Policy targets are only accepted by sources that index by policy id; run
    # this normalization check under a non-host_ch source so the host_ch policy
    # guard (see test below) doesn't reject it first.
    monkeypatch.setattr(
        "app.api.routers.contracts.get_settings",
        lambda: Settings(CHAIN_SOURCE="other"),
    )
    repo = FakeApiRepo()
    client = _client(repo)
    upper = ("b2" * 28).upper()
    r = client.post("/api/contracts", json={"target": upper})
    assert r.status_code == 200
    assert r.json()["target"] == upper.lower()
    assert repo.saved_contracts[0]["target"] == upper.lower()


def test_create_contract_rejects_policy_under_host_ch(monkeypatch: pytest.MonkeyPatch) -> None:
    # The host-backed source indexes by address only; a policy target must fail
    # fast (422) at create time rather than queue a job that deterministically
    # fails when it tries to fetch by policy id.
    monkeypatch.setattr(
        "app.api.routers.contracts.get_settings",
        lambda: Settings(CHAIN_SOURCE="host_ch"),
    )
    repo = FakeApiRepo()
    client = _client(repo)
    r = client.post("/api/contracts", json={"target": "b2" * 28})
    assert r.status_code == 422
    assert "policy" in r.json()["detail"].lower()
    assert repo.saved_contracts == []  # nothing queued


def test_non_ascii_api_key_header_is_401_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """hmac.compare_digest raises TypeError on non-ASCII str; the dependency must
    compare bytes so a crafted header fails closed with 401, not a 500."""
    monkeypatch.setattr(
        "app.api.deps.get_settings",
        lambda: Settings(API_KEY="secret"),
    )
    client = _client(FakeApiRepo(contracts=[]))
    # httpx refuses to encode non-ASCII str headers; send raw latin-1 bytes the
    # way a hostile client would put them on the wire.
    r = client.get("/api/contracts", headers={b"X-API-Key": b"\xff\xff\xff"})
    assert r.status_code == 401


def test_tx_label_routes_canonicalize_tx_hash_case() -> None:
    """Stored tx hashes are lowercase hex; an uppercase path param must write the
    label under the canonical hash, not a never-matching variant."""
    lower_tx = "ab" * 32
    repo = FakeApiRepo(contracts=[_contract_row()])
    client = _client(repo)
    r = client.post(
        f"/api/contracts/addr1a/transactions/{lower_tx.upper()}/label",
        json={"verdict": "benign"},
    )
    assert r.status_code == 200
    assert repo.label_calls == [("addr1a", [lower_tx], "benign")]
    r = client.post(f"/api/contracts/addr1a/transactions/{lower_tx.upper()}/clear-label", json={})
    assert r.status_code == 200
    assert repo.clear_calls == [("addr1a", [lower_tx])]
