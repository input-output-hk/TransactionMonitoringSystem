"""The canonical onboarding/refresh pipeline — the single path every contract
goes through (UI job worker, CLI, existing-contract backfill)."""

from __future__ import annotations

import logging
from typing import Any

from app.clustering.evaluate import evaluate
from app.config import get_settings
from app.ingest.ingester import ProgressFn, TargetKwargs, ingest
from app.registry import lookup_label
from app.service._common import (
    _MAX_ERROR_DETAIL,
    _MIN_TXS_FOR_ANALYSIS,
    _make_set_stage,
    _noop,
    _raise_if_incomplete,
    _recommended_params,
    _safe_error,
    load_clustering_input,
)
from app.service.analysis import _cluster_ci, _detect_ci
from app.service.history import get_history_backfill, history_cap, resolve_metadata
from app.sources.factory import get_source
from app.storage.protocol import Repo

logger = logging.getLogger(__name__)


async def process_contract(
    repo: Repo,
    *,
    target: str,
    target_type: str,
    max_txs: int | None,
    reprocess: bool = False,
    job_id: str | None = None,
    progress: ProgressFn = _noop,
) -> dict[str, Any]:
    """The canonical onboarding/refresh pipeline — the single path every contract
    goes through (UI job worker, CLI, existing-contract backfill).

    Stages: fetch metadata → (download unless ``reprocess`` or the source is
    host-backed) → shape cluster →
    shape anomaly → graph anomaly → mark done. Contracts with < 3 transactions
    skip clustering/anomaly and finish ``done`` with a note. When ``job_id`` is
    given, progress is also written to the ``jobs`` table for UI polling.
    """
    contract: dict[str, Any] = {
        "target": target,
        "target_type": target_type,
        "requested_max_txs": max_txs or 0,
        "target_txs": max_txs or 0,
        "status": "pending",
    }

    set_stage = _make_set_stage(repo, job_id, progress)
    settings = get_settings()
    # Outcome of the optional history-backfill stage ("" when it did not run);
    # read by the too-few-transactions branch below to keep the contract
    # retryable while its history is still outstanding.
    hist_status = ""

    try:
        async with get_source(settings) as source:
            set_stage("checking", f"fetching metadata for {target[:20]}…")
            meta = await resolve_metadata(source, settings, target, target_type)
            # A user-supplied name (pending row) or an existing label wins;
            # otherwise fall back to the registry. Keeps custom names through
            # reprocess while still labelling newly-recognised contracts.
            existing = repo.get_contract(target)
            preset = (existing or {}).get("label") or ""
            meta["label"] = preset or lookup_label(target, target_type)
            # The feed's refit jobs carry max_txs=0; without these lines a refit
            # would clobber the persisted per-contract backfill depth AND the
            # read/fit window back to 0, the same way `label` is preserved above.
            # Preserving target_txs is what keeps a refit from silently widening a
            # deliberately-narrowed window (or, for a legacy 0-row, from being a
            # no-op that leaves the ceiling intact).
            contract["requested_max_txs"] = max_txs or int(
                (existing or {}).get("requested_max_txs") or 0
            )
            contract["target_txs"] = max_txs or int((existing or {}).get("target_txs") or 0)
            contract.update(meta)
            contract["status"] = "processing"
            repo.save_contract(contract)

            # A host-backed source has nothing to download: its data already
            # lives in the host tables the engine reads via HostBackedRepo, and
            # it has no fetch_tx (calling the download path raises SourceError →
            # "upstream data provider error"). Skip discovery+download for it
            # regardless of the per-job reprocess flag — the fit reads features
            # straight from storage below. (getattr: stub sources in tests need
            # not declare the attribute; absent means "downloading", the default.)
            host_backed = getattr(source, "host_backed", False)

            # Optional pre-deployment history backfill (HISTORY_SOURCE set).
            # Runs REGARDLESS of `reprocess`: reprocess means "do not re-download
            # the primary data", while this stage is cursor-guarded (skip-fast)
            # so a refit re-entry costs one cursor read when complete — and the
            # refit is exactly the resume vehicle after a rate limit or deferral.
            # Never fatal: a deferred/rate-limited backfill must not fail the
            # onboard (the fit proceeds on the host's tip-forward data).
            if host_backed and settings.history_enabled:
                backfill = get_history_backfill(settings)
                if backfill is not None:
                    cap = history_cap(contract, settings)
                    set_stage("downloading", "backfilling pre-deployment history")
                    hist = await backfill.run(
                        target=target,
                        target_type=target_type,
                        max_txs=cap,
                        progress=lambda m: set_stage("downloading", m),
                    )
                    hist_status = hist.status
                    note = f" — {hist.note}" if hist.note else ""
                    set_stage(
                        "downloading",
                        f"history: {hist.status} ({hist.txs_ingested} txs){note}",
                    )

            if not reprocess and not host_backed:

                def on_download(msg: str) -> None:
                    set_stage("downloading", msg)

                set_stage("downloading", "starting download")
                target_kw: TargetKwargs = (
                    {"address": target} if target_type == "address" else {"policy_id": target}
                )
                # Capped onboarding ingests the most RECENT N txs, so the fitted
                # clusters/baselines reflect current traffic and the cursor lands
                # near the tip (classify then catches up from the window's end).
                ingest_result = await ingest(
                    repo=repo,
                    source=source,
                    max_txs=max_txs,
                    recent=bool(max_txs),
                    progress=on_download,
                    **target_kw,
                )
                # A rate-limited (partial) download must not be clustered or marked
                # done — re-raise so this becomes a failed, resumable job.
                _raise_if_incomplete(ingest_result)

        # Clustering / anomaly are synchronous (sklearn + ClickHouse); the
        # data-source client is closed before we get here. Each feature matrix is
        # built ONCE and reused across evaluate/cluster/anomaly for this target.
        set_stage("clustering", "evaluating shape parameters")
        shape_ci = load_clustering_input(repo, target, "shape")
        n = len(shape_ci.tx_hashes)
        result: dict[str, Any] = {"target": target, "target_type": target_type, "tx_count": n}

        if n < _MIN_TXS_FOR_ANALYSIS:
            # With history still outstanding (pending host job, deferred, or a
            # rate-limited walk), a `done` row would dead-end: a model-less done
            # contract is never re-onboarded by the feed (drift needs a model).
            # Stay `pending` so the feed retries once the history lands.
            history_outstanding = hist_status in ("pending", "deferred", "rate_limited")
            contract.update(status="pending" if history_outstanding else "done", tx_count=n)
            repo.save_contract(contract)
            suffix = "; history backfill outstanding, will retry" if history_outstanding else ""
            set_stage(
                "done",
                f"only {n} transaction(s); skipped clustering/anomaly{suffix}",
                txs_done=n,
            )
            return {**result, "note": "too few transactions for clustering/anomaly"}

        eps, min_samples = _recommended_params(evaluate(shape_ci))
        cluster = _cluster_ci(
            repo,
            target,
            shape_ci,
            eps,
            min_samples,
            notes="auto: process_contract",
            origin="system",
        )
        result["cluster_run_id"] = cluster["run_id"]

        set_stage("scoring", "shape anomaly detection")
        result["shape_anomaly_run_id"] = _detect_ci(repo, target, shape_ci, origin="system")[
            "run_id"
        ]
        set_stage("scoring", "graph anomaly detection")
        graph_ci = load_clustering_input(repo, target, "graph")
        result["graph_anomaly_run_id"] = _detect_ci(repo, target, graph_ci, origin="system")[
            "run_id"
        ]

        # Surface this fit's flagged verdicts to the TMS as contract_anomaly
        # rows (the host_ch sidecar path; a non-host_ch source would skip this).
        if get_settings().host_backed:
            from app.service.publish import publish_contract_anomaly

            publish_contract_anomaly(repo, target, network=get_settings().cardano_network)

        contract.update(status="done", tx_count=n)
        repo.save_contract(contract)
        set_stage("done", f"{n} txs · shape cluster + shape/graph anomaly", txs_done=n)
        return result
    except Exception as exc:
        logger.exception("process_contract failed for %s", target)
        # Preserve whatever metadata was last persisted (e.g. a prior 'done' row
        # on a failed reprocess) rather than clobbering it with defaults.
        try:
            existing = repo.get_contract(target)
        except Exception:  # pragma: no cover - best-effort
            existing = None
        failed = {**(existing or contract), "status": "failed"}
        try:
            repo.save_contract(failed)
        except Exception:  # pragma: no cover - best-effort status write
            logger.exception("failed to persist failed-contract status for %s", target)
        if job_id is not None:
            try:
                repo.update_job(
                    job_id,
                    status="failed",
                    error=_safe_error(exc)[:_MAX_ERROR_DETAIL],
                    stage_detail="",
                )
            except Exception:  # pragma: no cover
                logger.exception("failed to persist failed-job status for %s", job_id)
        raise
