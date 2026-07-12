"""Run DBSCAN on a ClusteringInput and persist the result."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.metrics import silhouette_score

from app.features import ClusteringInput
from app.ids import new_id
from app.storage.protocol import Repo


@dataclass(slots=True)
class DBSCANResult:
    tx_hashes: list[str]
    labels: np.ndarray
    n_points: int
    n_clusters: int
    n_noise: int
    silhouette: float  # NaN when undefined (< 2 clusters)


# Fixed seed for the silhouette subsample so repeated fits over the same window
# score identically (mirrors the fixed SVD random_state in features/graph.py).
_SILHOUETTE_SEED = 0


def silhouette_of(ci: ClusteringInput, labels: np.ndarray) -> float:
    """Silhouette score over non-noise points, or NaN when undefined.

    Above the SILHOUETTE_SAMPLE_SIZE cap the score is estimated on a fixed-seed
    subsample: the exact score is O(n^2) and is computed once per grid config,
    which is unaffordable at the clustering_window_txs population bound.
    """
    from app.config import get_settings  # late: avoid import cycles at module load

    mask = labels != -1
    if mask.sum() < 2:
        return math.nan
    sub_labels = labels[mask]
    if len(set(sub_labels.tolist())) < 2:
        return math.nan
    if ci.metric == "precomputed":
        data = ci.data[np.ix_(mask, mask)]
        metric = "precomputed"
    else:
        data = ci.data[mask]
        metric = "euclidean"
    cap = get_settings().silhouette_sample_size
    # Sentinel 0 = sampling disabled (see the Settings field); a cap at or above
    # the population is also exact since there is nothing to sample away.
    if not 0 < cap < len(sub_labels):
        return float(silhouette_score(data, sub_labels, metric=metric))
    try:
        return float(
            silhouette_score(
                data, sub_labels, metric=metric,
                sample_size=cap, random_state=_SILHOUETTE_SEED,
            )
        )
    except ValueError:
        # The subsample can drop every point of a cluster and leave one label
        # class, which sklearn rejects. A cluster too light to land a single
        # point in the sample has negligible weight in the exact score, so
        # report undefined rather than fail the fit or pay the O(n^2) fallback.
        return math.nan


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
    return new_id(feature_set)


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
    if ci.dropped_txs:
        # Surface the graph down-sample in the run's own metadata, not only in a
        # log line: whoever reads this run must see it was fit on a partial
        # (most-recent) slice of the window.
        drop_note = (
            f"graph sampling kept the {result.n_points} most recent txs, "
            f"dropped {ci.dropped_txs} older"
        )
        notes = f"{notes}; {drop_note}" if notes else drop_note
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
