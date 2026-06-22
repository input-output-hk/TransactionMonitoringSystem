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

    Stages: fetch metadata → (download unless ``reprocess``) → shape cluster →
    shape anomaly → graph anomaly → mark done. Contracts with < 3 transactions
    skip clustering/anomaly and finish ``done`` with a note. When ``job_id`` is
    given, progress is also written to the ``jobs`` table for UI polling.
    """
    contract: dict[str, Any] = {
        "target": target,
        "target_type": target_type,
        "requested_max_txs": max_txs or 0,
        "status": "pending",
    }

    set_stage = _make_set_stage(repo, job_id, progress)

    try:
        async with get_source(get_settings()) as source:
            set_stage("checking", f"fetching metadata for {target[:20]}…")
            meta = await source.metadata(target, target_type)
            # A user-supplied name (pending row) or an existing label wins;
            # otherwise fall back to the registry. Keeps custom names through
            # reprocess while still labelling newly-recognised contracts.
            existing = repo.get_contract(target)
            preset = (existing or {}).get("label") or ""
            meta["label"] = preset or lookup_label(target, target_type)
            contract.update(meta)
            contract["status"] = "processing"
            repo.save_contract(contract)

            if not reprocess:
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
                    repo=repo, source=source, max_txs=max_txs, recent=bool(max_txs),
                    progress=on_download, **target_kw
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
            contract.update(status="done", tx_count=n)
            repo.save_contract(contract)
            set_stage("done", f"only {n} transaction(s); skipped clustering/anomaly", txs_done=n)
            return {**result, "note": "too few transactions for clustering/anomaly"}

        eps, min_samples = _recommended_params(evaluate(shape_ci))
        cluster = _cluster_ci(
            repo, target, shape_ci, eps, min_samples,
            notes="auto: process_contract", origin="system",
        )
        result["cluster_run_id"] = cluster["run_id"]

        set_stage("scoring", "shape anomaly detection")
        result["shape_anomaly_run_id"] = _detect_ci(
            repo, target, shape_ci, origin="system"
        )["run_id"]
        set_stage("scoring", "graph anomaly detection")
        graph_ci = load_clustering_input(repo, target, "graph")
        result["graph_anomaly_run_id"] = _detect_ci(
            repo, target, graph_ci, origin="system"
        )["run_id"]

        # Surface this fit's flagged verdicts to the TMS as contract_anomaly
        # rows (the host_ch sidecar path; a non-host_ch source would skip this).
        if get_settings().chain_source == "host_ch":
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
                    job_id, status="failed",
                    error=_safe_error(exc)[:_MAX_ERROR_DETAIL], stage_detail="",
                )
            except Exception:  # pragma: no cover
                logger.exception("failed to persist failed-job status for %s", job_id)
        raise
