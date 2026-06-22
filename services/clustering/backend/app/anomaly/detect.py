"""Ensemble anomaly detection.

Combines three complementary, unsupervised detectors over a transaction feature
matrix and fuses them into a single consensus score:

  * Isolation Forest — global rarity in feature space;
  * Local Outlier Factor (LOF) — local density deviation;
  * DBSCAN noise — points outside every dense region.

Per-detector scores are rank-normalized to [0, 1] and averaged into a consensus
score; `votes` counts how many detectors independently flag the point (in their
top ``top_quantile``, or DBSCAN-noise). A point flagged by several detectors is
a far stronger candidate than any single detector's top pick.

NOTE: these surface *statistically anomalous* transactions for human review,
not provably malicious ones — there is no ground-truth label here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import rankdata
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor

from app.clustering.dbscan import run_dbscan
from app.clustering.evaluate import default_min_samples, k_distance
from app.features import ClusteringInput

DEFAULT_TOP_QUANTILE = 0.05
# Shared anomaly-detector hyperparameters (also reused by the persisted online
# model in clustering/model.py, so the batch and online detectors stay aligned).
LOF_NEIGHBORS = 20
ISO_ESTIMATORS = 300
# Fallback eps when no knee is found, chosen by metric: Jaccard distances live in
# [0, 1] while Euclidean is unbounded, so they warrant different defaults.
_FALLBACK_EPS_PRECOMPUTED = 0.5
_FALLBACK_EPS_EUCLIDEAN = 1.0
# A transaction is "flagged" once at least this many detectors independently vote
# for it — a consensus signal far stronger than any single detector's top pick.
FLAG_VOTE_THRESHOLD = 2


@dataclass(slots=True)
class AnomalyResult:
    tx_hashes: list[str]
    iso: np.ndarray  # higher = more anomalous; NaN where not applicable
    lof: np.ndarray
    dbscan_noise: np.ndarray  # 0/1
    consensus: np.ndarray  # [0, 1]
    votes: np.ndarray  # int 0..3
    rank: np.ndarray  # 1 = most anomalous
    methods: list[str]
    eps: float
    min_samples: int


def _rank_norm(x: np.ndarray) -> np.ndarray:
    """Rank-percentile normalize to [0, 1] (robust to scale/outliers)."""
    n = len(x)
    if n <= 1:
        return np.zeros(n)
    return (rankdata(x, method="average") - 1.0) / (n - 1.0)


def detect_anomalies(
    ci: ClusteringInput,
    *,
    eps: float | None = None,
    min_samples: int | None = None,
    top_quantile: float = DEFAULT_TOP_QUANTILE,
    lof_neighbors: int = LOF_NEIGHBORS,
    random_state: int = 0,
) -> AnomalyResult:
    n = len(ci.tx_hashes)
    if n == 0:
        empty = np.array([])
        return AnomalyResult([], empty, empty, empty, empty, empty, empty, [], 0.0, 0)

    if min_samples is None:
        min_samples = default_min_samples(ci)
    if eps is None:
        knee = k_distance(ci, min_samples)["knee_eps"]
        eps = knee or (
            _FALLBACK_EPS_PRECOMPUTED if ci.metric == "precomputed" else _FALLBACK_EPS_EUCLIDEAN
        )

    methods: list[str] = []
    precomputed = ci.metric == "precomputed"

    # Isolation Forest needs feature vectors, so it is skipped for precomputed
    # distance matrices.
    if precomputed:
        iso = np.full(n, np.nan)
    else:
        iso_model = IsolationForest(
            n_estimators=ISO_ESTIMATORS, random_state=random_state, contamination="auto"
        ).fit(ci.data)
        iso = -iso_model.score_samples(ci.data)
        methods.append("isolation_forest")

    k = max(2, min(lof_neighbors, n - 1))
    lof_model = LocalOutlierFactor(
        n_neighbors=k, metric="precomputed" if precomputed else "minkowski"
    )
    lof_model.fit_predict(ci.data)
    lof = -lof_model.negative_outlier_factor_
    methods.append("lof")

    result = run_dbscan(ci, eps=eps, min_samples=min_samples)
    dbscan_noise = (result.labels == -1).astype(int)
    methods.append("dbscan")

    # Fuse: average rank-normalized signals; count per-detector "flag" votes.
    lof_norm = _rank_norm(lof)
    signals = [lof_norm, dbscan_noise.astype(float)]
    threshold = 1.0 - top_quantile
    votes = dbscan_noise.copy()
    votes = votes + (lof_norm >= threshold).astype(int)
    if not precomputed:
        iso_norm = _rank_norm(iso)
        signals.insert(0, iso_norm)
        votes = votes + (iso_norm >= threshold).astype(int)

    consensus = np.mean(np.vstack(signals), axis=0)

    order = np.argsort(-consensus, kind="stable")
    rank = np.empty(n, dtype=int)
    rank[order] = np.arange(1, n + 1)

    return AnomalyResult(
        tx_hashes=list(ci.tx_hashes),
        iso=iso,
        lof=lof,
        dbscan_noise=dbscan_noise,
        consensus=consensus,
        votes=votes,
        rank=rank,
        methods=methods,
        eps=float(eps),
        min_samples=int(min_samples),
    )
