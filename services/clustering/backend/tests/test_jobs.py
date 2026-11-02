"""JobManager status transitions with a stubbed process_contract (no network).

The pipeline is replaced with an async stub so we exercise queueing, the worker
loop and the queued→done / exception→failed transitions deterministically.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from app.config import get_settings
from app.jobs import JobManager
from tests.fakes import FakeRepoBase

# Bound on how long a test waits for the worker thread, and how often it looks.
_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.01


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

    async def stub(
        r: Any, *, target: str, target_type: str, max_txs: Any, reprocess: bool, job_id: str
    ) -> None:
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

    async def boom(
        r: Any, *, target: str, target_type: str, max_txs: Any, reprocess: bool, job_id: str
    ) -> None:
        assert max_txs is None  # 0 (unbounded) becomes None
        raise RuntimeError("address not found")

    monkeypatch.setattr("app.jobs.process_contract", boom)
    JobManager(repo_factory=lambda: repo)._run_job("job-2")
    assert repo.jobs["job-2"]["status"] == "failed"
    # The backstop persists the CLIENT-SAFE error, not the raw exception text:
    # the raw message must not leak into the jobs table, but it must point to
    # the server logs where the full message is recorded.
    persisted = repo.jobs["job-2"]["error"]
    assert "address not found" not in persisted
    assert "see server logs" in persisted


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


class FlakyJobRepo(FakeJobRepo):
    """Fails the first ``fail_writes`` status writes, like a ClickHouse that is
    out of memory for a moment."""

    def __init__(self, fail_writes: int) -> None:
        super().__init__()
        self.fail_writes = fail_writes
        self.write_attempts = 0

    def update_job(self, job_id: str, **changes: Any) -> None:
        self.write_attempts += 1
        if self.write_attempts <= self.fail_writes:
            raise RuntimeError("MEMORY_LIMIT_EXCEEDED")
        super().update_job(job_id, **changes)


async def _fail(r: Any, **kwargs: Any) -> None:
    raise RuntimeError("MEMORY_LIMIT_EXCEEDED")


@pytest.fixture
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_FAILED_WRITE_RETRY_SECONDS", "0")
    get_settings.cache_clear()


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(_POLL_SECONDS)


@pytest.mark.usefixtures("no_retry_wait")
def test_failure_write_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = get_settings().job_failed_write_attempts
    repo = FlakyJobRepo(fail_writes=attempts - 1)
    repo.create_job("job-5", "addr1x", "address", 0, 0)
    monkeypatch.setattr("app.jobs.process_contract", _fail)

    JobManager(repo_factory=lambda: repo)._run_job("job-5")

    assert repo.jobs["job-5"]["status"] == "failed"
    assert repo.write_attempts == attempts


@pytest.mark.usefixtures("no_retry_wait")
def test_failure_write_gives_up_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = get_settings().job_failed_write_attempts
    repo = FlakyJobRepo(fail_writes=attempts)
    repo.create_job("job-6", "addr1x", "address", 0, 0)
    monkeypatch.setattr("app.jobs.process_contract", _fail)

    JobManager(repo_factory=lambda: repo)._run_job("job-6")

    assert repo.jobs["job-6"]["status"] == "queued"
    assert repo.write_attempts == attempts
    assert repo.closed == 1


@pytest.mark.usefixtures("no_retry_wait")
def test_a_job_whose_failure_was_not_recorded_runs_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mainnet case: the run fails and so does every write of its failure,
    leaving the job non-terminal with no worker. The reaper puts it back and
    the second run settles it, so its contract is scored again."""
    repo = FlakyJobRepo(fail_writes=get_settings().job_failed_write_attempts)
    repo.create_job("job-7", "addr1x", "address", 0, 0)
    runs: list[str] = []

    async def fail_then_succeed(r: Any, *, job_id: str, **kwargs: Any) -> None:
        runs.append(job_id)
        if len(runs) == 1:
            raise RuntimeError("MEMORY_LIMIT_EXCEEDED")
        r.update_job(job_id, status="done")

    monkeypatch.setattr("app.jobs.process_contract", fail_then_succeed)
    mgr = JobManager(repo_factory=lambda: repo)
    mgr.start()
    try:
        _wait_until(lambda: len(runs) == 1 and not mgr._owned)
        assert repo.jobs["job-7"]["status"] == "queued"

        with mgr.enqueue_lock:
            assert mgr.reap_orphans(repo) == ["job-7"]
        _wait_until(lambda: repo.jobs["job-7"]["status"] == "done")
    finally:
        mgr.stop()
    assert runs == ["job-7", "job-7"]


def test_reaper_leaves_queued_and_running_jobs_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeJobRepo()
    repo.create_job("job-8", "addr1a", "address", 0, 0)
    repo.create_job("job-9", "addr1b", "address", 0, 0)
    running = threading.Event()
    release = threading.Event()

    async def block(r: Any, *, job_id: str, **kwargs: Any) -> None:
        running.set()
        release.wait(timeout=_WAIT_SECONDS)
        r.update_job(job_id, status="done")

    monkeypatch.setattr("app.jobs.process_contract", block)
    mgr = JobManager(repo_factory=lambda: repo)
    mgr.start()  # one job running, the other waiting in the queue
    try:
        assert running.wait(timeout=_WAIT_SECONDS)
        with mgr.enqueue_lock:
            assert mgr.reap_orphans(repo) == []
    finally:
        release.set()
        _wait_until(lambda: not mgr._owned)
        mgr.stop()
    assert {j["status"] for j in repo.jobs.values()} == {"done"}
