"""On-demand analysis entry points: evaluate parameters, run DBSCAN, run the
anomaly ensemble — each over a freshly loaded or a pre-built ``ClusteringInput``.
"""

from __future__ import annotations

import math
from typing import Any

from app.anomaly.detect import DEFAULT_TOP_QUANTILE, FLAG_VOTE_THRESHOLD, detect_anomalies
from app.clustering.dbscan import new_run_id, persist_run, run_dbscan
from app.clustering.evaluate import evaluate
from app.service._common import load_clustering_input
from app.storage.protocol import Repo


def evaluate_target(repo: Repo, target: str, feature_set: str) -> dict[str, Any]:
    ci = load_clustering_input(repo, target, feature_set)
    return evaluate(ci)


def cluster_target(
    repo: Repo,
    target: str,
    feature_set: str,
    eps: float,
    min_samples: int,
    *,
    notes: str = "",
    origin: str = "custom",
) -> dict[str, Any]:
    ci = load_clustering_input(repo, target, feature_set)
    return _cluster_ci(repo, target, ci, eps, min_samples, notes=notes, origin=origin)


def _cluster_ci(
    repo: Repo,
    target: str,
    ci: Any,
    eps: float,
    min_samples: int,
    *,
    notes: str = "",
    origin: str = "custom",
) -> dict[str, Any]:
    """Cluster a pre-built ``ClusteringInput`` (avoids reloading features when the
    caller already has the matrix in hand)."""
    feature_set = ci.feature_set
    run_id = new_run_id(feature_set)
    result = run_dbscan(ci, eps=eps, min_samples=min_samples)
    persist_run(
        repo,
        run_id=run_id,
        target=target,
        ci=ci,
        eps=eps,
        min_samples=min_samples,
        result=result,
        notes=notes,
        origin=origin,
    )
    return {
        "run_id": run_id,
        "target": target,
        "feature_set": feature_set,
        "eps": eps,
        "min_samples": min_samples,
        "n_points": result.n_points,
        "n_clusters": result.n_clusters,
        "n_noise": result.n_noise,
        "silhouette": None if math.isnan(result.silhouette) else result.silhouette,
        "origin": origin,
    }


def detect_anomalies_for_target(
    repo: Repo,
    target: str,
    feature_set: str,
    *,
    eps: float | None = None,
    min_samples: int | None = None,
    top_quantile: float = DEFAULT_TOP_QUANTILE,
    origin: str = "custom",
) -> dict[str, Any]:
    ci = load_clustering_input(repo, target, feature_set)
    return _detect_ci(
        repo,
        target,
        ci,
        eps=eps,
        min_samples=min_samples,
        top_quantile=top_quantile,
        origin=origin,
    )


def _detect_ci(
    repo: Repo,
    target: str,
    ci: Any,
    *,
    eps: float | None = None,
    min_samples: int | None = None,
    top_quantile: float = DEFAULT_TOP_QUANTILE,
    origin: str = "custom",
) -> dict[str, Any]:
    """Run anomaly detection on a pre-built ``ClusteringInput`` and persist it."""
    feature_set = ci.feature_set
    result = detect_anomalies(ci, eps=eps, min_samples=min_samples, top_quantile=top_quantile)
    run_id = "anomaly-" + new_run_id(feature_set)
    rows = [
        (
            result.tx_hashes[i],
            float(result.iso[i]),
            float(result.lof[i]),
            int(result.dbscan_noise[i]),
            float(result.consensus[i]),
            int(result.votes[i]),
            int(result.rank[i]),
        )
        for i in range(len(result.tx_hashes))
    ]
    n_flagged = int((result.votes >= FLAG_VOTE_THRESHOLD).sum()) if result.votes.size else 0
    repo.save_anomaly_run(
        {
            "run_id": run_id,
            "target": target,
            "feature_set": feature_set,
            "methods": ",".join(result.methods),
            "n_points": len(result.tx_hashes),
            "n_flagged": n_flagged,
            "eps": result.eps,
            "min_samples": result.min_samples,
            "top_quantile": top_quantile,
            "origin": origin,
        }
    )
    repo.save_anomaly_scores(run_id, rows)
    return {
        "run_id": run_id,
        "target": target,
        "feature_set": feature_set,
        "methods": result.methods,
        "n_points": len(result.tx_hashes),
        "n_flagged": n_flagged,
        "eps": result.eps,
        "min_samples": result.min_samples,
    }
