"""JobManager status transitions with a stubbed process_contract (no network).

The pipeline is replaced with an async stub so we exercise queueing, the worker
loop and the queued→done / exception→failed transitions deterministically.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from app.jobs import JobManager
from tests.fakes import FakeRepoBase


class FakeJobRepo(FakeRepoBase):
    """In-memory stand-in for the parts of ClickHouseRepo the worker touches."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.closed = 0

    def create_job(
        self, job_id: str, target: str, target_type: str, max_txs: int, reprocess: int
    ) -> None:
        self.jobs[job_id] = {
            "job_id": job_id,
            "target": target,
            "target_type": target_type,
            "max_txs": max_txs,
            "reprocess": reprocess,
            "status": "queued",
            "stage_detail": "",
            "txs_done": 0,
            "error": "",
        }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    def update_job(self, job_id: str, **changes: Any) -> None:
        self.jobs[job_id].update(changes)

    def nonterminal_jobs(self) -> list[dict[str, Any]]:
        return [dict(j) for j in self.jobs.values() if j["status"] not in ("done", "failed")]

    def close(self) -> None:
        self.closed += 1


def test_run_job_success(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeJobRepo()
    repo.create_job("job-1", "addr1x", "address", 100, 0)

    async def stub(r: Any, *, target: str, target_type: str, max_txs: Any,
                   reprocess: bool, job_id: str) -> None:
        assert max_txs == 100  # 100 passed through unchanged
        r.update_job(job_id, status="done", txs_done=42)

    monkeypatch.setattr("app.jobs.process_contract", stub)
    JobManager(repo_factory=lambda: repo)._run_job("job-1")
    assert repo.jobs["job-1"]["status"] == "done"
    assert repo.jobs["job-1"]["txs_done"] == 42
    assert repo.closed == 1


def test_run_job_exception_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeJobRepo()
    repo.create_job("job-2", "addrbad", "address", 0, 0)

    async def boom(r: Any, *, target: str, target_type: str, max_txs: Any,
                   reprocess: bool, job_id: str) -> None:
        assert max_txs is None  # 0 (unbounded) becomes None
        raise RuntimeError("address not found")

    monkeypatch.setattr("app.jobs.process_contract", boom)
    JobManager(repo_factory=lambda: repo)._run_job("job-2")
    assert repo.jobs["job-2"]["status"] == "failed"
    assert "address not found" in repo.jobs["job-2"]["error"]


def test_run_job_skips_terminal_job(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeJobRepo()
    repo.create_job("job-3", "addr1x", "address", 0, 0)
    repo.update_job("job-3", status="done")

    calls = {"n": 0}

    async def stub(r: Any, **kwargs: Any) -> None:
        calls["n"] += 1

    monkeypatch.setattr("app.jobs.process_contract", stub)
    JobManager(repo_factory=lambda: repo)._run_job("job-3")
    assert calls["n"] == 0  # already terminal -> not reprocessed


def test_start_reenqueues_and_worker_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeJobRepo()
    repo.create_job("job-4", "addr1x", "address", 0, 0)  # queued -> should re-enqueue
    done = threading.Event()

    async def stub(r: Any, *, job_id: str, **kwargs: Any) -> None:
        r.update_job(job_id, status="done")
        done.set()

    monkeypatch.setattr("app.jobs.process_contract", stub)
    mgr = JobManager(repo_factory=lambda: repo)
    mgr.start()
    try:
        assert done.wait(timeout=5)
    finally:
        mgr.stop()
    assert repo.jobs["job-4"]["status"] == "done"
