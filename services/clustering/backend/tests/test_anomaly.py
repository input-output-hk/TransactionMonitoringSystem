"""Tests for ensemble anomaly detection on synthetic data with injected outliers."""

from __future__ import annotations

import numpy as np
from scipy.spatial.distance import pdist, squareform

from app.anomaly.detect import detect_anomalies
from app.features import ClusteringInput

# Indices of the two deliberately-injected far outliers.
OUTLIERS = {60, 61}


def _data_with_outliers() -> ClusteringInput:
    rng = np.random.default_rng(0)
    normal = rng.normal(0.0, 0.3, size=(60, 2))
    outliers = np.array([[20.0, 20.0], [-20.0, 18.0]])
    X = np.vstack([normal, outliers])
    tx = [f"tx{i}" for i in range(X.shape[0])]
    return ClusteringInput(tx, X, "euclidean", "shape", ["f0", "f1"])


def test_detect_ranks_outliers_first() -> None:
    res = detect_anomalies(_data_with_outliers(), top_quantile=0.05)
    assert set(res.methods) == {"isolation_forest", "lof", "dbscan"}
    top3 = set(np.argsort(res.rank)[:3])  # indices with smallest rank == most anomalous
    assert OUTLIERS <= top3
    for i in OUTLIERS:
        assert res.votes[i] >= 2  # flagged by multiple detectors
        assert res.consensus[i] > 0.8
    # a central normal point should score low
    assert res.consensus[0] < res.consensus[60]


def test_detect_precomputed_skips_isolation_forest() -> None:
    base = _data_with_outliers()
    distance = squareform(pdist(base.data))
    ci = ClusteringInput(base.tx_hashes, distance, "precomputed", "graph", ["jaccard"])
    res = detect_anomalies(ci, top_quantile=0.05)
    assert "isolation_forest" not in res.methods
    assert np.isnan(res.iso).all()
    top3 = set(np.argsort(res.rank)[:3])
    assert OUTLIERS & top3  # outliers still surface via LOF + DBSCAN


def test_detect_empty_input() -> None:
    ci = ClusteringInput([], np.empty((0, 0)), "euclidean", "shape", [])
    res = detect_anomalies(ci)
    assert res.tx_hashes == []
    assert res.consensus.size == 0
