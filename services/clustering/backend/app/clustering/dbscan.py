"""Run DBSCAN on a ClusteringInput and persist the result."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.metrics import silhouette_score

from app.features import ClusteringInput
from app.storage.protocol import Repo


@dataclass(slots=True)
class DBSCANResult:
    tx_hashes: list[str]
    labels: np.ndarray
    n_points: int
    n_clusters: int
    n_noise: int
    silhouette: float  # NaN when undefined (< 2 clusters)


def silhouette_of(ci: ClusteringInput, labels: np.ndarray) -> float:
    """Silhouette score over non-noise points, or NaN when undefined."""
    mask = labels != -1
    if mask.sum() < 2:
        return math.nan
    sub_labels = labels[mask]
    if len(set(sub_labels.tolist())) < 2:
        return math.nan
    if ci.metric == "precomputed":
        sub = ci.data[np.ix_(mask, mask)]
        return float(silhouette_score(sub, sub_labels, metric="precomputed"))
    return float(silhouette_score(ci.data[mask], sub_labels, metric="euclidean"))


def run_dbscan(ci: ClusteringInput, eps: float, min_samples: int) -> DBSCANResult:
    n_points = len(ci.tx_hashes)
    if n_points == 0:
        return DBSCANResult([], np.array([], dtype=int), 0, 0, 0, math.nan)

    model = DBSCAN(eps=eps, min_samples=min_samples, metric=ci.metric)
    labels = model.fit_predict(ci.data)
    unique = set(labels.tolist())
    n_clusters = len(unique - {-1})
    n_noise = int(np.sum(labels == -1))
    return DBSCANResult(
        tx_hashes=list(ci.tx_hashes),
        labels=labels,
        n_points=n_points,
        n_clusters=n_clusters,
        n_noise=n_noise,
        silhouette=silhouette_of(ci, labels),
    )


def new_run_id(feature_set: str) -> str:
    return f"{feature_set}-{uuid.uuid4().hex[:12]}"


def persist_run(
    repo: Repo,
    *,
    run_id: str,
    target: str,
    ci: ClusteringInput,
    eps: float,
    min_samples: int,
    result: DBSCANResult,
    notes: str = "",
    origin: str = "custom",
) -> None:
    # Write the labels BEFORE the run row so that any reader which sees the run
    # (e.g. service.ensure_shape_model via latest_cluster_run) always finds its
    # membership fully present — avoids fitting a model on a half-written run.
    repo.save_cluster_labels(
        run_id,
        list(zip(result.tx_hashes, result.labels.tolist(), strict=True)),
    )
    repo.save_cluster_run(
        {
            "run_id": run_id,
            "target": target,
            "feature_set": ci.feature_set,
            "eps": float(eps),
            "min_samples": int(min_samples),
            "metric": ci.metric,
            "n_points": result.n_points,
            "n_clusters": result.n_clusters,
            "n_noise": result.n_noise,
            "silhouette": result.silhouette,
            "notes": notes,
            "origin": origin,
        }
    )
