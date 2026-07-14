"""Automatic feed for the clustering module (CHAIN_SOURCE=host_ch).

The TMS ingests the whole chain; this loop is what makes a *watched* contract
get scored automatically as its new transactions arrive, with no manual fetch
step. Each tick it reads the watchlist (the ``contracts`` registry) and, for
every contract that has no job already running, enqueues work through the
single-worker ``JobManager`` (no new concurrency primitive, single writer
preserved):

- a contract with no model yet (``pending``) -> an onboard fit (``reprocess`` so
  there is no download: ``process_contract`` reads the host's data via
  ``HostBackedRepo``);
- a fitted contract whose online drift has crossed
  ``recluster_noise_threshold`` -> a windowed re-fit (cheap, convergent);
- an otherwise-fitted contract -> an incremental ``classify`` of its new txs.

Per-tick work is capped (``FEED_MAX_CONTRACTS_PER_TICK``) so a large watchlist
cannot flood the worker. The job pipelines publish ``tx_contract_anomaly`` for
the host on completion (see ``service.publish``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import Settings
from app.ids import new_id
from app.jobs import JobManager, RepoFactory
from app.service._common import target_in_jobs

logger = logging.getLogger(__name__)


def _decide(contract: dict[str, Any], settings: Settings) -> tuple[str, int] | None:
    """(kind, reprocess) for a watched contract, or None to skip this tick.

    ``processing``/``failed`` are left alone (in-flight, or a persistent error a
    blind retry would just loop on); a human re-adds or the operator inspects."""
    status = contract.get("status")
    if status == "pending":
        return ("onboard", 1)  # initial fit; reprocess => no download (host_ch)
    if status == "done":
        drift = float(contract.get("drift_score") or 0.0)
        if settings.recluster_recommended(drift):
            return ("onboard", 1)  # windowed re-fit on sustained drift
        return ("classify", 0)
    return None


def feed_tick(*, manager: JobManager, repo_factory: RepoFactory, settings: Settings) -> int:
    """Enqueue work for up to ``FEED_MAX_CONTRACTS_PER_TICK`` non-busy watched
    contracts. Returns the number of jobs enqueued. Owns its repo."""
    repo = repo_factory()
    enqueued = 0
    try:
        busy = {j["target"] for j in repo.nonterminal_jobs()}
        cap = settings.feed_max_contracts_per_tick
        for contract in repo.list_contracts():
            if enqueued >= cap:
                break
            target = contract["target"]
            if target in busy:
                continue
            decision = _decide(contract, settings)
            if decision is None:
                continue
            kind, reprocess = decision
            # Serialize the read-check-create-enqueue against the API's own
            # enqueue path so two writers can't both pass the busy guard.
            with manager.enqueue_lock:
                if target_in_jobs(repo.nonterminal_jobs(), target):
                    continue
                job_id = new_id("job")
                repo.create_job(
                    job_id,
                    target,
                    contract.get("target_type", "address"),
                    0,
                    reprocess,
                    kind=kind,
                )
                manager.enqueue(job_id)
            enqueued += 1
            logger.debug("feed enqueued %s job for %s", kind, target[:24])
    finally:
        repo.close()
    return enqueued


async def run_feed(
    *,
    manager: JobManager,
    repo_factory: RepoFactory,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    """Poll-and-enqueue loop until ``stop_event`` is set. Each tick's errors are
    logged and swallowed so a transient ClickHouse blip never kills the feed."""
    interval = settings.feed_poll_interval_seconds
    logger.info(
        "clustering feed started: interval=%ds, max_per_tick=%d",
        interval,
        settings.feed_max_contracts_per_tick,
    )
    while not stop_event.is_set():
        try:
            n = feed_tick(manager=manager, repo_factory=repo_factory, settings=settings)
            if n:
                logger.info("feed tick enqueued %d job(s)", n)
        except Exception:
            logger.exception("feed tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass
    logger.info("clustering feed stopped")
