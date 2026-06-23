"""Incremental classification (fit/score split).

The *fit* half persists a frozen ClusterModel from a batch run; the *score* half
classifies NEW transactions against it without re-running DBSCAN. The manual
precursor to real-time streaming classification — see
docs/online-classification-design.md. Shape feature set only for now.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clustering.model import (
    MODEL_SCHEMA_VERSION,
    build_shape_model,
    load_cluster_model,
    score_shape,
    serialize_model,
)
from app.config import get_settings
from app.ids import new_id
from app.ingest.ingester import ProgressFn, TargetKwargs, ingest
from app.service._common import (
    _CLASSIFY_BATCH,
    _MAX_ERROR_DETAIL,
    _make_set_stage,
    _noop,
    _raise_if_incomplete,
    _safe_error,
)
from app.service.verdicts import VERDICT_ANOMALY, VERDICT_MALICIOUS, compute_verdicts
from app.sources.factory import get_source
from app.storage.protocol import Repo

logger = logging.getLogger(__name__)


def ensure_shape_model(repo: Repo, target: str) -> dict[str, Any] | None:
    """Return the latest persisted shape model, building it lazily from the most
    recent shape cluster run if none exists. ``None`` if the contract has no run
    yet (e.g. too few transactions to cluster)."""
    # Fit from the canonical (system-tuned) run so a user's custom clustering
    # never silently becomes the online model. Fall back to the latest run of any
    # origin for pre-migration / system-less targets.
    run = repo.latest_canonical_run(target, "shape") or repo.latest_cluster_run(target, "shape")
    existing = repo.latest_cluster_model(target, "shape")
    # Reuse the existing model only if it is current-schema AND built from the
    # latest cluster run; a re-cluster (new run_id) or a schema bump invalidates
    # it and forces a rebuild. NOTE: a manual relabel *without* a re-cluster does
    # not refresh the model's verdict snapshot — that needs a re-cluster (see
    # docs/online-classification-design.md, "Not yet").
    if (
        existing
        and int(existing.get("schema_version", -1)) == MODEL_SCHEMA_VERSION
        and (run is None or existing.get("run_id") == run["run_id"])
    ):
        return existing
    if run is None:
        return None
    cluster_of = repo.run_tx_labels(run["run_id"])
    if not cluster_of:
        return None

    shape_df = repo.fetch_shape_features(target)
    train_df = shape_df[shape_df["tx_hash"].isin(set(cluster_of))].reset_index(drop=True)
    explicit = repo.labels_for_target(target)
    # Only cluster-applied labels define a cluster's frozen verdict; a single-tx
    # (manual) label must not bake the whole cluster as malicious into the model.
    _, cluster_info = compute_verdicts(
        cluster_of, explicit, {}, propagating=repo.cluster_labeled_hashes(target)
    )
    cluster_verdicts = {
        cid: info["verdict"] for cid, info in cluster_info.items() if info["verdict"]
    }
    model = build_shape_model(
        train_df=train_df,
        cluster_of=cluster_of,
        cluster_verdicts=cluster_verdicts,
        eps=float(run["eps"]),
        min_samples=int(run["min_samples"]),
    )
    model_id = new_id("model")
    repo.save_cluster_model(
        {
            "model_id": model_id,
            "target": target,
            "feature_set": "shape",
            "run_id": run["run_id"],
            "schema_version": MODEL_SCHEMA_VERSION,
            "n_clusters": model.n_clusters,
            "n_train": len(train_df),
            "eps": float(run["eps"]),
            "min_samples": int(run["min_samples"]),
            "blob": serialize_model(model),
        }
    )
    return repo.latest_cluster_model(target, "shape")


def _classify_result(
    repo: Repo, target: str, model_id: str, *, n_new: int, n_flagged: int
) -> dict[str, Any]:
    """Assemble the classify result and refresh the drift sensor.

    The trailing online-noise rate is recomputed even when nothing new was scored:
    it reflects the existing classifications and may already warrant a re-cluster
    (e.g. a contract that drifted before this code shipped). Computed over a
    trailing window (not just the current batch) so the signal is stable and
    recovers as fresh traffic is scored — see Repo.online_noise_rate."""
    drift_score, drift_n = repo.online_noise_rate(
        target, "shape", model_id, window=get_settings().online_noise_window,
    )
    return {
        "target": target,
        "model_id": model_id,
        "n_new": n_new,
        "n_flagged": n_flagged,
        "drift_score": drift_score,
        "drift_window_n": drift_n,
    }


def classify_new_transactions(repo: Repo, target: str) -> dict[str, Any]:
    """Classify the target's not-yet-classified transactions against its current
    shape model (building the model on first use). O(clusters) per transaction —
    no DBSCAN re-run."""
    model_row = ensure_shape_model(repo, target)
    if model_row is None:
        return {"target": target, "n_new": 0, "note": "no cluster model yet; run full analysis first"}

    model_id = model_row["model_id"]
    new_hashes = repo.unclassified_tx_hashes(
        target, "shape", run_id=model_row["run_id"], model_id=model_id
    )
    if not new_hashes:
        return _classify_result(repo, target, model_id, n_new=0, n_flagged=0)

    model = load_cluster_model(model_row)
    n_new = 0
    n_flagged = 0
    # Score in chunks so the per-fetch IN(...) array and in-memory matrix stay
    # bounded when a contract has a large backlog of unclassified transactions.
    for start in range(0, len(new_hashes), _CLASSIFY_BATCH):
        chunk = new_hashes[start : start + _CLASSIFY_BATCH]
        classifications = score_shape(model, repo.fetch_shape_features_for(target, chunk))
        repo.save_tx_classifications(
            [
                (
                    target, c.tx_hash, "shape", model_id, c.cluster_id,
                    c.iso_score, c.lof_score, c.votes, c.consensus, c.verdict,
                )
                for c in classifications
            ]
        )
        n_new += len(classifications)
        n_flagged += sum(
            1 for c in classifications if c.verdict in (VERDICT_ANOMALY, VERDICT_MALICIOUS)
        )
    return _classify_result(repo, target, model_id, n_new=n_new, n_flagged=n_flagged)


async def update_contract(
    repo: Repo,
    *,
    target: str,
    target_type: str,
    job_id: str | None = None,
    progress: ProgressFn = _noop,
) -> dict[str, Any]:
    """Incremental refresh: download new transactions from the tip and classify
    only those against the contract's frozen model. Does NOT re-cluster history.
    Shares the ``jobs`` status enum (downloading → scoring → done) with onboarding.

    NOTE: this resolves ``get_source`` in *this* module, so tests stubbing the data
    provider for the online path patch ``app.service.online.get_source`` (the batch
    path patches ``app.service.pipeline.get_source``).
    """

    set_stage = _make_set_stage(repo, job_id, progress)
    settings = get_settings()
    # Integrated sidecar (CHAIN_SOURCE=host_ch): the host already ingested the
    # chain, so there is no download — the repo's feature reads come from the
    # host tables. Skip the tip walk and classify directly against what the host
    # has; the feed scheduler is what makes this "incremental".
    host_backed = settings.chain_source == "host_ch"

    try:
        if not host_backed:
            set_stage("downloading", "fetching new transactions")
            target_kw: TargetKwargs = (
                {"address": target} if target_type == "address" else {"policy_id": target}
            )
            async with get_source(settings) as source:
                ingest_result = await ingest(
                    repo=repo,
                    source=source,
                    from_tip=True,
                    resume=True,
                    max_txs=None,
                    progress=lambda m: set_stage("downloading", m),
                    **target_kw,
                )
            # If the tip walk is rate-limited it stops short of the tip with the
            # cursor saved (done=False); don't classify a partial catch-up or mark
            # the job done — fail so a re-run resumes and classifies everything.
            _raise_if_incomplete(ingest_result)

        set_stage("scoring", "classifying new transactions")
        out = classify_new_transactions(repo, target)
        if host_backed:
            # Surface the freshly-classified verdicts to the host TMS.
            from app.service.publish import publish_contract_anomaly

            publish_contract_anomaly(repo, target, network=settings.cardano_network)

        drift_score: float | None = out.get("drift_score")

        contract = repo.get_contract(target)
        if contract is not None:
            contract["tx_count"] = repo.count_transactions(target)
            contract["status"] = "done"
            if drift_score is not None:
                contract["drift_score"] = drift_score
            repo.save_contract(contract)

        if out["n_new"]:
            detail = f"classified {out['n_new']} new tx(s)" + (
                f" · {out['n_flagged']} flagged" if out.get("n_flagged") else ""
            )
        else:
            detail = out.get("note") or "no new transactions"
        if drift_score is not None and get_settings().recluster_recommended(drift_score):
            detail += (
                f" · model drift high ({round(drift_score * 100)}% unassigned)"
                " — re-cluster recommended"
            )
        set_stage("done", detail, txs_done=out["n_new"])
        return out
    except Exception as exc:
        logger.exception("update_contract failed for %s", target)
        if job_id is not None:
            try:
                repo.update_job(
                    job_id, status="failed", error=_safe_error(exc)[:_MAX_ERROR_DETAIL]
                )
            except Exception:  # pragma: no cover
                pass
        raise
