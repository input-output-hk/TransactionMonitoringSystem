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
import time
from typing import Any

from app.config import _COVERAGE_UNKNOWN, Settings
from app.ids import new_id
from app.jobs import JobManager, RepoFactory
from app.service._common import target_in_jobs
from app.storage.protocol import iter_all_rows

logger = logging.getLogger(__name__)


def _decide(contract: dict[str, Any], settings: Settings, *, now: int) -> tuple[str, int] | None:
    """(kind, reprocess) for a watched contract, or None to skip this tick.

    ``processing``/``failed`` are left alone (in-flight, or a persistent error a
    blind retry would just loop on); a human re-adds or the operator inspects.

    For a ``done`` contract the drift-driven re-fit is gated two ways so it cannot
    become the old non-convergent loop:
      * clusterability: an un-clusterable fit (``model_unclusterable``) re-fits to
        the same majority-noise model, so re-clustering on drift is futile. Such a
        contract only re-baselines on the slow cadence below (never the ~60s loop),
        and otherwise falls through to ``classify`` (which still scores + publishes
        every tick, so recall is untouched);
      * anti-flap: no contract is auto-re-fit more than once per
        ``feed_refit_min_interval_seconds``. A drift that appears once the previous
        fit is older than the interval re-fits on the next tick; a drift that
        appears sooner has its RE-FIT deferred up to the interval. Either way this
        is recall-safe: classify + IsolationForest/LOF novelty scoring run on every
        tick against the frozen model, so a deferred re-fit only delays baseline
        refresh (which shows up as false positives, never missed detections)."""
    status = contract.get("status")
    if status == "pending":
        return ("onboard", 1)  # initial fit; reprocess => no download (host_ch)
    if status != "done":
        return None
    drift = float(contract.get("drift_score") or 0.0)
    fit_coverage = float(contract.get("fit_coverage", _COVERAGE_UNKNOWN))
    last_fit_at = int(contract.get("last_fit_at") or 0)
    # last_fit_at 0 (never fit / legacy) leaves throttled False, so a first re-fit
    # is always allowed; the interval only collapses REPEAT auto-re-fits.
    throttled = last_fit_at > 0 and (now - last_fit_at) < settings.feed_refit_min_interval_seconds
    if settings.model_unclusterable(fit_coverage):
        # Structurally un-clusterable: re-fit only on the slow re-baseline cadence
        # to keep detector thresholds fresh, never the tight drift loop.
        return ("onboard", 1) if not throttled else ("classify", 0)
    if settings.recluster_recommended(drift, fit_coverage) and not throttled:
        return ("onboard", 1)  # clusterable + genuinely stale: a re-fit converges
    return ("classify", 0)


def feed_tick(*, manager: JobManager, repo_factory: RepoFactory, settings: Settings) -> int:
    """Enqueue work for up to ``FEED_MAX_CONTRACTS_PER_TICK`` non-busy watched
    contracts. Returns the number of jobs enqueued. Owns its repo."""
    repo = repo_factory()
    enqueued = 0
    # One clock read per tick, shared by every _decide call, so the anti-flap
    # interval is evaluated against a single consistent "now".
    now = int(time.time())
    try:
        busy = {j["target"] for j in repo.nonterminal_jobs()}
        cap = settings.feed_max_contracts_per_tick
        # Page through the WHOLE registry (list_contracts now paginates with a
        # default page of 100): a watched contract beyond the first page must
        # still be scored, or its attacks would silently go unwatched.
        for contract in iter_all_rows(repo.list_contracts):
            if enqueued >= cap:
                break
            target = contract["target"]
            if target in busy:
                continue
            decision = _decide(contract, settings, now=now)
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
