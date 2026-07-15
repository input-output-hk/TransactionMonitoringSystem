"""Parameter evaluation for DBSCAN — the "decide the parameters" step.

Provides:
  * ``k_distance`` — the sorted k-nearest-neighbour distance curve plus an
    auto-detected knee, the classic heuristic for choosing ``eps``;
  * ``grid_search`` — scores an ``eps x min_samples`` grid by silhouette,
    cluster count and noise ratio;
  * ``evaluate`` — combines both and returns a recommended ``(eps, min_samples)``.

All return values are JSON-serializable so the API can hand them to the UI.
"""

from __future__ import annotations

import math
from itertools import product
from typing import Any

import numpy as np
from kneed import KneeLocator
from sklearn.neighbors import NearestNeighbors

from app import tunables
from app.clustering.dbscan import run_dbscan
from app.features import ClusteringInput

# Values live in config/clustering.yaml (section `evaluation`), validated at
# import by app.tunables; the constant names below are unchanged so call sites
# and cross-module imports read exactly as before.
_EVALUATION = tunables.get("evaluation")

# Minimum points before a parameter search is meaningful (DBSCAN needs a few).
MIN_POINTS: int = int(_EVALUATION["min_points"])
# Plotting cap: the k-distance curve is downsampled to this many points.
_MAX_CURVE_POINTS: int = int(_EVALUATION["max_curve_points"])
# Fallback eps when the k-distance knee is undefined, and the percentile of the
# k-distance curve used to derive it when KneeLocator finds no knee.
# FALLBACK_EPS is public: the service pipeline imports it as its own
# last-resort eps when neither the grid nor the knee produced one.
FALLBACK_EPS: float = float(_EVALUATION["fallback_eps"])
_KNEE_FALLBACK_PERCENTILE: int = int(_EVALUATION["knee_fallback_percentile"])
# eps grid for the precomputed-Jaccard metric (distances live in [0, 1]).
_PRECOMPUTED_EPS_GRID: list[float] = [float(v) for v in _EVALUATION["precomputed_eps_grid"]]
# Multipliers applied to the k-distance knee to build the Euclidean eps grid.
_EPS_MULTIPLIERS: list[float] = [float(v) for v in _EVALUATION["eps_multipliers"]]
# min_samples heuristics: precomputed default, and the 2*n_features rule clamped
# to [floor, ceil]; the grid also probes the floor and a capped 2*base.
# MIN_SAMPLES_FLOOR is public: the service pipeline imports it as its heuristic
# min_samples fallback when the grid has no recommendation.
_PRECOMPUTED_MIN_SAMPLES: int = int(_EVALUATION["precomputed_min_samples"])
MIN_SAMPLES_FLOOR: int = int(_EVALUATION["min_samples_floor"])
_MIN_SAMPLES_CEIL: int = int(_EVALUATION["min_samples_ceil"])
_MIN_SAMPLES_GRID_CAP: int = int(_EVALUATION["min_samples_grid_cap"])
# A recommended config needs at least this many clusters and below this noise
# fraction to be considered viable.
_MIN_CLUSTERS: int = int(_EVALUATION["min_clusters"])
_MAX_NOISE_RATIO: float = float(_EVALUATION["max_noise_ratio"])


def _knn_kth_distances(ci: ClusteringInput, k: int) -> np.ndarray:
    """Distance from each point to its k-th nearest neighbour (self included)."""
    n = len(ci.tx_hashes)
    k = max(2, min(k, n - 1))
    metric = "precomputed" if ci.metric == "precomputed" else "euclidean"
    nn = NearestNeighbors(n_neighbors=k, metric=metric)
    nn.fit(ci.data)
    distances, _ = nn.kneighbors(ci.data)
    return np.sort(distances[:, -1])


def k_distance(ci: ClusteringInput, k: int) -> dict[str, Any]:
    """Sorted k-distance curve + auto-detected knee (suggested eps)."""
    if len(ci.tx_hashes) < MIN_POINTS:
        return {"k": k, "distances": [], "knee_eps": None}

    sorted_dist = _knn_kth_distances(ci, k)
    x = np.arange(len(sorted_dist))
    knee_eps: float | None = None
    try:
        locator = KneeLocator(x, sorted_dist, curve="convex", direction="increasing")
        if locator.knee is not None:
            knee_eps = float(sorted_dist[int(locator.knee)])
    except Exception:  # pragma: no cover - kneed can be finicky on tiny inputs
        knee_eps = None
    if knee_eps is None or knee_eps <= 0:
        knee_eps = float(np.percentile(sorted_dist, _KNEE_FALLBACK_PERCENTILE))

    # Downsample for plotting while preserving the curve shape.
    if len(sorted_dist) > _MAX_CURVE_POINTS:
        idx = np.linspace(0, len(sorted_dist) - 1, _MAX_CURVE_POINTS).astype(int)
        curve = sorted_dist[idx]
    else:
        curve = sorted_dist
    return {"k": k, "distances": [float(v) for v in curve], "knee_eps": knee_eps}


def grid_search(
    ci: ClusteringInput, eps_values: list[float], min_samples_values: list[int]
) -> list[dict[str, Any]]:
    n = len(ci.tx_hashes)
    results: list[dict[str, Any]] = []
    for eps, ms in product(eps_values, min_samples_values):
        res = run_dbscan(ci, eps=eps, min_samples=ms)
        results.append(
            {
                "eps": round(float(eps), 6),
                "min_samples": int(ms),
                "n_clusters": res.n_clusters,
                "n_noise": res.n_noise,
                "noise_ratio": round(res.n_noise / n, 4) if n else 0.0,
                "silhouette": None if math.isnan(res.silhouette) else round(res.silhouette, 4),
            }
        )
    return results


def _eps_grid(ci: ClusteringInput, knee_eps: float) -> list[float]:
    if ci.metric == "precomputed":  # Jaccard distance lives in [0, 1]
        return list(_PRECOMPUTED_EPS_GRID)
    return [round(knee_eps * m, 6) for m in _EPS_MULTIPLIERS if knee_eps * m > 0]


def default_min_samples(ci: ClusteringInput) -> int:
    """Heuristic DBSCAN ``min_samples``: a fixed default for the precomputed-Jaccard
    metric, else ``2 * n_features`` clamped to ``[MIN_SAMPLES_FLOOR, _MIN_SAMPLES_CEIL]``.
    Shared by grid search and the anomaly detector so both agree on the default
    neighbourhood size."""
    if ci.metric == "precomputed":
        return _PRECOMPUTED_MIN_SAMPLES
    n_features = ci.data.shape[1] if ci.data.ndim == 2 else 1
    return int(min(max(2 * n_features, MIN_SAMPLES_FLOOR), _MIN_SAMPLES_CEIL))


def _min_samples_grid(ci: ClusteringInput) -> tuple[int, list[int]]:
    n = len(ci.tx_hashes)
    base = default_min_samples(ci)
    candidates = sorted({MIN_SAMPLES_FLOOR, base, min(base * 2, _MIN_SAMPLES_GRID_CAP)})
    candidates = [c for c in candidates if 2 <= c <= max(2, n - 1)]
    return base, (candidates or [min(MIN_SAMPLES_FLOOR, max(2, n - 1))])


def _recommend(grid: list[dict[str, Any]], knee_eps: float, base_ms: int) -> dict[str, Any]:
    # A good config has ≥2 clusters, isn't almost-all noise, and has a defined
    # silhouette. Zero noise is fine (often ideal), so there is no lower bound.
    viable = [
        r
        for r in grid
        if r["n_clusters"] >= _MIN_CLUSTERS
        and r["noise_ratio"] < _MAX_NOISE_RATIO
        and r["silhouette"] is not None
    ]
    if viable:
        best = max(viable, key=lambda r: (r["silhouette"], -r["noise_ratio"]))
        return {
            "eps": best["eps"],
            "min_samples": best["min_samples"],
            "rationale": (
                f"highest silhouette among configs with ≥{_MIN_CLUSTERS} clusters "
                f"and <{_MAX_NOISE_RATIO:.0%} noise"
            ),
        }
    return {
        "eps": round(knee_eps, 6),
        "min_samples": base_ms,
        "rationale": "fallback: k-distance knee + heuristic min_samples (no clear grid winner)",
    }


def evaluate(ci: ClusteringInput) -> dict[str, Any]:
    n = len(ci.tx_hashes)
    n_features = (
        int(ci.data.shape[1]) if (ci.metric != "precomputed" and ci.data.ndim == 2) else None
    )
    if n < MIN_POINTS:
        return {
            "feature_set": ci.feature_set,
            "metric": ci.metric,
            "n_points": n,
            "n_features": n_features,
            "k_distance": {"k": 0, "distances": [], "knee_eps": None},
            "grid": [],
            "recommended": None,
            "message": f"Not enough transactions to evaluate (need ≥ {MIN_POINTS}).",
        }

    base_ms, ms_grid = _min_samples_grid(ci)
    kd = k_distance(ci, base_ms)
    knee_eps = kd["knee_eps"] or FALLBACK_EPS
    eps_grid = _eps_grid(ci, knee_eps)
    grid = grid_search(ci, eps_grid, ms_grid)
    recommended = _recommend(grid, knee_eps, base_ms)
    return {
        "feature_set": ci.feature_set,
        "metric": ci.metric,
        "n_points": n,
        "n_features": n_features,
        "k_distance": kd,
        "grid": grid,
        "recommended": recommended,
    }
