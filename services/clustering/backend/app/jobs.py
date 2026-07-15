"""Background job runner for the contract-onboarding pipeline.

A single daemon worker thread pops ``job_id`` values off a queue and runs the
async ``process_contract`` pipeline on its own event loop, so the FastAPI event
loop never blocks on the long, sync-ClickHouse-heavy download/clustering work.
Each job uses its own ``ClickHouseRepo`` (the ClickHouse client is not
thread-safe). Requires a single API process — already the case under
``uvicorn app.api.main:app``.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Callable

from app.service import process_contract, update_contract
from app.service._common import _MAX_ERROR_DETAIL, _safe_error
from app.storage.clickhouse import ClickHouseRepo
from app.storage.protocol import Repo

logger = logging.getLogger(__name__)

# Sentinel pushed on stop() to unblock the worker's blocking queue.get().
_STOP = object()

RepoFactory = Callable[[], Repo]


class JobManager:
    """Queue + one daemon worker thread driving ``process_contract``."""

    def __init__(self, repo_factory: RepoFactory = ClickHouseRepo) -> None:
        self._queue: queue.Queue[object] = queue.Queue()
        self._repo_factory = repo_factory
        self._thread: threading.Thread | None = None
        # Held by the API around the read-check-create-enqueue sequence so two
        # concurrent POSTs can't both pass the "already running"/max-inflight
        # guard for the same target (the endpoints run on a threadpool).
        self.enqueue_lock = threading.Lock()

    def start(self) -> None:
        """Re-enqueue any non-terminal jobs (ingest resumes from its cursor),
        then spin up the worker thread."""
        repo = self._repo_factory()
        try:
            for job in repo.nonterminal_jobs():
                self._queue.put(job["job_id"])
        except Exception:  # pragma: no cover - startup resilience
            logger.exception("failed to re-enqueue non-terminal jobs")
        finally:
            repo.close()
        self._thread = threading.Thread(target=self._worker, name="job-worker", daemon=True)
        self._thread.start()

    def enqueue(self, job_id: str) -> None:
        # Respawn the worker if it ever died, so jobs never pile up undrained.
        if self._thread is None or not self._thread.is_alive():
            logger.warning("job worker not alive; respawning")
            self._thread = threading.Thread(target=self._worker, name="job-worker", daemon=True)
            self._thread.start()
        self._queue.put(job_id)

    def stop(self) -> None:
        self._queue.put(_STOP)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            # Guard the whole iteration: a malformed item or an error escaping
            # _run_job must not silently kill the only worker thread. (A bare
            # BaseException like SystemExit is intentionally not caught; the
            # respawn-on-enqueue check is the backstop for a dead thread.)
            try:
                if not isinstance(item, str):
                    logger.error("dropping non-str queue item %r", item)
                    continue
                self._run_job(item)
            except Exception:
                logger.exception("worker iteration failed for %r", item)

    def _run_job(self, job_id: str) -> None:
        """Run one job to completion. Owns its repo; never raises."""
        repo = self._repo_factory()
        try:
            job = repo.get_job(job_id)
            if job is None:
                logger.warning("job %s not found; skipping", job_id)
                return
            if job["status"] in ("done", "failed"):
                return
            if job.get("kind") == "classify":
                # Incremental refresh: download new txs from the tip + score them
                # against the frozen model (no full re-cluster).
                asyncio.run(
                    update_contract(
                        repo,
                        target=job["target"],
                        target_type=job["target_type"],
                        job_id=job_id,
                    )
                )
            else:
                max_txs = job["max_txs"] or None  # 0 means unbounded
                asyncio.run(
                    process_contract(
                        repo,
                        target=job["target"],
                        target_type=job["target_type"],
                        max_txs=max_txs,
                        reprocess=bool(job["reprocess"]),
                        job_id=job_id,
                    )
                )
        except Exception as exc:  # process_contract already recorded failure; backstop here
            logger.exception("job %s failed", job_id)
            try:
                # Persist the client-safe error (not raw str(exc), which can leak
                # internals), capped by the shared limit — matches process_contract's
                # own failure path so the jobs table never carries a raw message.
                repo.update_job(
                    job_id,
                    status="failed",
                    error=_safe_error(exc)[:_MAX_ERROR_DETAIL],
                )
            except Exception:  # pragma: no cover
                pass
        finally:
            repo.close()
