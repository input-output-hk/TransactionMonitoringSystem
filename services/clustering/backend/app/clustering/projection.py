"""Project the clustering feature matrix to 2-D / 3-D for visualization.

DBSCAN clusters in the *full* feature space (13 dims for ``shape``, ≈21 for
``combined``, or a precomputed Jaccard distance matrix for ``graph``). The
co-spend graph view, by contrast, lays nodes out by **address** topology, which
need not reflect that space — so feature-space clusters can be invisible there.

This module produces low-dimensional coordinates *of the clustering input
itself*, so on-screen proximity reflects the distances DBSCAN actually used:

  * euclidean feature sets (``shape`` / ``combined``) → PCA (standardized first);
  * precomputed Jaccard distance (``graph``)          → classical MDS (PCoA) of
    the distance matrix, via double-centering + a top-k eigendecomposition.

Both are deterministic (``random_state=0`` / a closed-form eigensolve) so a
run's projection is stable across requests.

For the PCA path we also surface *what each axis means*: every principal
component is a linear combination of the input features, so we report its
explained-variance fraction and the features with the largest |loading| (signed).
MDS axes have no feature interpretation, so they carry no loadings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.sparse.linalg import eigsh
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

MAX_DIMS = 3
# How many top-loading features to attribute to each PCA axis.
TOP_FEATURES_PER_AXIS = 3


@dataclass(slots=True)
class AxisInfo:
    """Interpretation of one projected axis. ``variance`` is the fraction of total
    variance the axis explains (PCA) or ``None`` (MDS, where axes only preserve
    distance). ``top_features`` are ``(feature_name, signed_loading)`` pairs, largest
    |loading| first — empty for MDS or when feature names are unavailable."""

    variance: float | None = None
    top_features: list[tuple[str, float]] = field(default_factory=list)


def project(
    data: np.ndarray,
    metric: str,
    dims: int,
    feature_names: list[str] | None = None,
) -> tuple[np.ndarray, list[AxisInfo]]:
    """Project ``data`` to ``dims`` axes and describe each axis.

    Returns ``(coords, axes)`` where ``coords`` is ``(n, dims)`` and ``axes`` has one
    ``AxisInfo`` per displayed axis. ``data`` is an ``(n, features)`` feature matrix
    for euclidean metrics or an ``(n, n)`` distance matrix for ``"precomputed"``;
    ``dims`` is clamped to ``[1, MAX_DIMS]`` and coords are zero-padded to exactly
    ``dims`` columns so callers always get a stable shape.
    """
    dims = max(1, min(int(dims), MAX_DIMS))
    n = int(data.shape[0]) if data.ndim >= 1 else 0
    if n == 0:
        return np.empty((0, dims)), [AxisInfo() for _ in range(dims)]
    # Can't place more independent axes than we have points.
    k = min(dims, n)

    if metric == "precomputed":
        coords, axes = _mds(data, k), [AxisInfo() for _ in range(k)]
    else:
        coords, axes = _pca(data, k, feature_names)

    if coords.shape[1] < dims:
        coords = np.hstack([coords, np.zeros((n, dims - coords.shape[1]))])
    axes += [AxisInfo() for _ in range(dims - len(axes))]  # pad to dims
    return coords, axes


def project_data(data: np.ndarray, metric: str, dims: int) -> np.ndarray:
    """Coordinates only — convenience wrapper over :func:`project`."""
    return project(data, metric, dims)[0]


def _top_features(loadings: np.ndarray, names: list[str] | None) -> list[tuple[str, float]]:
    """The ``TOP_FEATURES_PER_AXIS`` features with the largest |loading|, signed."""
    if not names or len(names) != len(loadings):
        return []
    order = np.argsort(np.abs(loadings))[::-1][:TOP_FEATURES_PER_AXIS]
    return [(names[i], round(float(loadings[i]), 3)) for i in order]


def _pca(
    X: np.ndarray, k: int, names: list[str] | None
) -> tuple[np.ndarray, list[AxisInfo]]:
    if X.ndim != 2 or X.shape[1] == 0:
        return np.zeros((X.shape[0], k)), [AxisInfo() for _ in range(k)]
    k = min(k, X.shape[1])
    X = StandardScaler().fit_transform(X.astype(np.float64))
    # All-identical (or single) rows have no variance — PCA would divide by zero
    # computing the explained-variance ratio. Collapse them onto the origin.
    if not np.any(X.std(axis=0) > 0):
        return np.zeros((X.shape[0], k)), [AxisInfo() for _ in range(k)]
    pca = PCA(n_components=k, random_state=0)
    coords = pca.fit_transform(X)
    axes = [
        AxisInfo(float(pca.explained_variance_ratio_[i]), _top_features(pca.components_[i], names))
        for i in range(k)
    ]
    return coords, axes


def _mds(D: np.ndarray, k: int) -> np.ndarray:
    """Classical (Torgerson) MDS of a precomputed distance matrix.

    Double-centre the squared distances into a Gram matrix ``B`` (done in O(n²)
    via row/column means rather than a dense ``J B J`` product), then take the top
    ``k`` eigenpairs. ``eigsh`` (Lanczos, top-k only) keeps this fast on the
    O(few-thousand)-node matrices the projection view caps at; tiny inputs fall
    back to a dense ``eigh`` since ``eigsh`` requires ``k < n - 1``.
    """
    D2 = np.asarray(D, dtype=np.float64) ** 2
    row_mean = D2.mean(axis=1, keepdims=True)
    col_mean = D2.mean(axis=0, keepdims=True)
    grand = float(D2.mean())
    B = -0.5 * (D2 - row_mean - col_mean + grand)
    B = (B + B.T) / 2.0  # symmetrise against floating-point drift

    n = B.shape[0]
    if n <= k + 1:
        vals, vecs = np.linalg.eigh(B)
    else:
        vals, vecs = eigsh(B, k=k, which="LA")
    order = np.argsort(vals)[::-1][:k]
    vals_k = np.clip(vals[order], 0.0, None)
    return vecs[:, order] * np.sqrt(vals_k)
