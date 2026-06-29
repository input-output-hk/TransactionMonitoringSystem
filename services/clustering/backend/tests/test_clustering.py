"""Tests for DBSCAN clustering and parameter evaluation on synthetic data."""

from __future__ import annotations

import numpy as np

from app.clustering.dbscan import run_dbscan
from app.clustering.evaluate import evaluate
from app.clustering.projection import MAX_DIMS, project_data
from app.features import ClusteringInput


def _blobs_matrix() -> np.ndarray:
    """Two well-separated 5-D blobs (30 points each)."""
    rng = np.random.default_rng(0)
    a = rng.normal(loc=0.0, scale=0.3, size=(30, 5))
    b = rng.normal(loc=10.0, scale=0.3, size=(30, 5))
    return np.vstack([a, b])


def _euclidean_distance_matrix(X: np.ndarray) -> np.ndarray:
    diff = X[:, None, :] - X[None, :, :]
    return np.sqrt((diff**2).sum(axis=-1))


def _is_separated(coords: np.ndarray) -> bool:
    """The two blobs (rows 0:30 vs 30:60) stay farther apart than they spread."""
    ca, cb = coords[:30].mean(axis=0), coords[30:].mean(axis=0)
    sep = float(np.linalg.norm(ca - cb))
    spread = max(
        float(np.linalg.norm(coords[:30] - ca, axis=1).max()),
        float(np.linalg.norm(coords[30:] - cb, axis=1).max()),
    )
    return sep > spread


def _two_blobs() -> ClusteringInput:
    rng = np.random.default_rng(0)
    a = rng.normal(loc=0.0, scale=0.3, size=(30, 2))
    b = rng.normal(loc=10.0, scale=0.3, size=(30, 2))
    X = np.vstack([a, b])
    tx_hashes = [f"tx{i}" for i in range(X.shape[0])]
    return ClusteringInput(tx_hashes, X, "euclidean", "shape", ["f0", "f1"])


def test_run_dbscan_finds_two_clusters() -> None:
    ci = _two_blobs()
    result = run_dbscan(ci, eps=1.0, min_samples=5)
    assert result.n_clusters == 2
    assert result.n_noise <= 3
    assert result.silhouette == result.silhouette  # not NaN
    assert result.silhouette > 0.8


def test_run_dbscan_empty_input() -> None:
    ci = ClusteringInput([], np.empty((0, 0)), "euclidean", "shape", [])
    result = run_dbscan(ci, eps=1.0, min_samples=5)
    assert result.n_points == 0
    assert result.n_clusters == 0


def test_evaluate_recommends_parameters() -> None:
    report = evaluate(_two_blobs())
    assert report["n_points"] == 60
    assert report["k_distance"]["knee_eps"] is not None
    assert report["grid"]
    assert report["recommended"] is not None
    assert report["recommended"]["min_samples"] >= 2
    # Two cleanly separated blobs must yield a real grid winner, not the fallback.
    assert "silhouette" in report["recommended"]["rationale"]


def test_evaluate_too_few_points() -> None:
    ci = ClusteringInput(["a", "b"], np.zeros((2, 2)), "euclidean", "shape", ["f0", "f1"])
    report = evaluate(ci)
    assert report["recommended"] is None
    assert "message" in report


# --- project_data: feature-space projection (PCA + classical MDS) ------------


def test_project_data_pca_separates_blobs() -> None:
    X = _blobs_matrix()
    for dims in (2, 3):
        coords = project_data(X, "euclidean", dims)
        assert coords.shape == (60, dims)
        assert np.isfinite(coords).all()
        assert _is_separated(coords)


def test_project_data_mds_precomputed_separates_blobs() -> None:
    # The graph feature set has no vectors — only a precomputed distance matrix —
    # so it goes through the classical-MDS branch. Structure must survive.
    D = _euclidean_distance_matrix(_blobs_matrix())
    for dims in (2, 3):
        coords = project_data(D, "precomputed", dims)
        assert coords.shape == (60, dims)
        assert np.isfinite(coords).all()
        assert _is_separated(coords)


def test_project_data_clamps_dims_and_pads() -> None:
    # dims is clamped to MAX_DIMS, and when fewer axes than dims are available
    # (here a single feature column) the result is zero-padded to exactly `dims`.
    X = _blobs_matrix()[:, :1]
    over = project_data(X, "euclidean", MAX_DIMS + 2)
    assert over.shape == (60, MAX_DIMS)
    assert np.allclose(over[:, 1:], 0.0)  # only one real axis; rest padded


def test_project_data_degenerate_inputs() -> None:
    assert project_data(np.empty((0, 5)), "euclidean", 2).shape == (0, 2)
    assert project_data(np.empty((0, 0)), "precomputed", 3).shape == (0, 3)
    # A single point (and identical/zero-variance points) collapse onto the origin.
    assert project_data(np.array([[1.0, 2.0, 3.0]]), "euclidean", 2).tolist() == [[0.0, 0.0]]
