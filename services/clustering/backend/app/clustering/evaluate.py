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
# Percentiles of the sorted k-distance curve used as whale-insensitive eps
# anchors for the Euclidean cluster grid; the tail is winsorised above
# _EPS_TAIL_CLIP_PERCENTILE before a second, robust knee is located. Both go
# through _knn_kth_distances and leave k_distance() (and thus the anomaly
# detector's eps) unchanged.
_EPS_PERCENTILES: list[float] = [float(v) for v in _EVALUATION["eps_percentiles"]]
_EPS_TAIL_CLIP_PERCENTILE: float = float(_EVALUATION["eps_tail_clip_percentile"])
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
# Above this fraction of all points in one cluster, a config is a degenerate
# mega-cluster (its silhouette is inflated by a few far specks); _recommend
# prefers a non-dominant split when one exists.
_MAX_DOMINANT_CLUSTER_RATIO: float = float(_EVALUATION["max_dominant_cluster_ratio"])


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


def _top_cluster_ratio(labels: np.ndarray, n: int) -> float:
    """Fraction of ALL points in the single largest non-noise cluster (same
    denominator as noise_ratio). ~1.0 means one mega-cluster swallowed the
    cloud; ~1/n_clusters means a balanced split. Lets the recommender reject
    degenerate configs whose silhouette is inflated by a few far specks."""
    if n == 0 or labels.size == 0:
        return 0.0
    non_noise = labels[labels != -1]
    if non_noise.size == 0:
        return 0.0
    return round(int(np.bincount(non_noise).max()) / n, 4)


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
                "top_cluster_ratio": _top_cluster_ratio(res.labels, n),
            }
        )
    return results


def _robust_eps_anchors(ci: ClusteringInput, k: int) -> list[float]:
    """Whale-insensitive Euclidean eps anchors.

    Recomputes the same sorted k-distance curve ``k_distance`` uses (leaving
    ``k_distance`` — and thus the anomaly detector's eps — byte-for-byte
    unchanged) and derives eps candidates that reflect the dense behavioural
    core rather than a heavy (whale) distance tail:

      1. percentile anchors of the raw curve (``eps_percentiles``): the dense
         core IS the bulk of the distribution, so these land at the scale the
         main cloud splits at even when the tail drags the KneeLocator knee far
         above it;
      2. a robust knee: KneeLocator over the curve winsorised above
         ``eps_tail_clip_percentile``, scaled by ``eps_multipliers`` — the
         whales no longer lever the elbow upward.

    Returns positive candidates (unsorted; the caller merges/dedups)."""
    sorted_dist = _knn_kth_distances(ci, k)
    anchors: list[float] = [float(np.percentile(sorted_dist, p)) for p in _EPS_PERCENTILES]

    clip_at = float(np.percentile(sorted_dist, _EPS_TAIL_CLIP_PERCENTILE))
    clipped = np.minimum(sorted_dist, clip_at)
    robust_knee: float | None = None
    # A flat curve (every k-distance equal, e.g. a tiny/degenerate window) has no
    # knee, and KneeLocator's 0..1 rescale would divide by zero on it; skip it.
    if float(np.ptp(clipped)) > 0:
        try:
            loc = KneeLocator(
                np.arange(len(clipped)), clipped, curve="convex", direction="increasing"
            )
            if loc.knee is not None:
                robust_knee = float(clipped[int(loc.knee)])
        except Exception:  # pragma: no cover - kneed is finicky on tiny inputs
            robust_knee = None
    if robust_knee is None or robust_knee <= 0:
        robust_knee = clip_at  # de-whaled fallback (not the raw tail-inflated knee)

    anchors.extend(robust_knee * m for m in _EPS_MULTIPLIERS)
    return [v for v in anchors if v > 0]


def _eps_grid(ci: ClusteringInput, knee_eps: float, k: int) -> list[float]:
    if ci.metric == "precomputed":  # Jaccard distance lives in [0, 1]
        return list(_PRECOMPUTED_EPS_GRID)
    # Today's raw-knee multiplier points (kept as a superset so no well-behaved
    # target regresses) UNION robust anchors that recover the dense-core scale
    # when a whale tail inflates the raw knee. The dominance gate in _recommend
    # discards the degenerate high-eps points this may add.
    raw_points = [knee_eps * m for m in _EPS_MULTIPLIERS]
    return sorted({round(v, 6) for v in (raw_points + _robust_eps_anchors(ci, k)) if v > 0})


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
        # Prefer configs whose largest cluster does NOT swallow the cloud: a
        # genuine multi-modal split over a mega-cluster whose silhouette is
        # inflated by a few far specks. Only if no such config exists (a
        # genuinely single-mode target) fall back to plain silhouette-max, so
        # recall-first we never force a fragmentation. Deliberately NO silhouette
        # floor on the non-dominant tier: a real split of adjacent modes can
        # score low (e.g. 0.38) yet still be the right answer.
        non_dominant = [r for r in viable if r["top_cluster_ratio"] < _MAX_DOMINANT_CLUSTER_RATIO]
        pool = non_dominant or viable
        best = max(pool, key=lambda r: (r["silhouette"], -r["noise_ratio"]))
        if non_dominant:
            rationale = (
                f"highest silhouette among configs with ≥{_MIN_CLUSTERS} clusters, "
                f"<{_MAX_NOISE_RATIO:.0%} noise and largest cluster "
                f"<{_MAX_DOMINANT_CLUSTER_RATIO:.0%} of points"
            )
        else:
            rationale = (
                f"highest silhouette among configs with ≥{_MIN_CLUSTERS} clusters and "
                f"<{_MAX_NOISE_RATIO:.0%} noise (no split below "
                f"{_MAX_DOMINANT_CLUSTER_RATIO:.0%} dominance; single-mode target)"
            )
        return {
            "eps": best["eps"],
            "min_samples": best["min_samples"],
            "rationale": rationale,
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
    eps_grid = _eps_grid(ci, knee_eps, base_ms)
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
