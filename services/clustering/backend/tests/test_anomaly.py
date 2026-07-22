"""Tests for ensemble anomaly detection on synthetic data with injected outliers."""

from __future__ import annotations

import numpy as np
from scipy.spatial.distance import pdist, squareform

from app.anomaly.detect import detect_anomalies
from app.config import Settings
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


def test_detect_flags_attack_within_the_default_window() -> None:
    # Recall guard for the per-contract default N (clustering_default_target_txs).
    # A brand-new contract onboarded with no explicit N fits/scores over the
    # default "latest N", which is smaller than the clustering_window_txs ceiling
    # that every contract used before this feature. The recall-first rule requires
    # proving a real attack still fires at that narrowed size, so plant far
    # outliers in a normal cloud of exactly the SHIPPED default N and assert they
    # surface in the flagged band, with the detectors' baselines intact (LOF's
    # fixed neighborhood, DBSCAN min_samples: N must be well above them).
    #
    # Scope: this proves detection at the default window SIZE. It does not (and
    # cannot) remove the inherent residual that an attack OLDER than the newest N
    # ages out of the window; an operator onboarding a contract whose
    # pre-deployment history exceeds N and who needs full historical recall must
    # set a larger N. That residual is the same class as the pre-existing ceiling,
    # just at a lower threshold.
    fields = Settings.model_fields
    default_n = fields["clustering_default_target_txs"].default
    # The default must be a real window, never lifted away by the recall floor.
    assert default_n >= fields["clustering_min_target_txs"].default

    rng = np.random.default_rng(0)
    normal = rng.normal(0.0, 0.3, size=(default_n, 2))
    outliers = np.array([[20.0, 20.0], [-20.0, 18.0]])
    X = np.vstack([normal, outliers])
    tx = [f"tx{i}" for i in range(X.shape[0])]
    ci = ClusteringInput(tx, X, "euclidean", "shape", ["f0", "f1"])

    res = detect_anomalies(ci, top_quantile=0.05)
    planted = {default_n, default_n + 1}  # the two injected outliers' indices
    top3 = set(np.argsort(res.rank)[:3])  # smallest rank == most anomalous
    assert planted <= top3  # the attack ranks among the most anomalous
    for i in planted:
        assert res.votes[i] >= 2  # flagged by multiple detectors
        assert res.consensus[i] > 0.8


def test_detect_empty_input() -> None:
    ci = ClusteringInput([], np.empty((0, 0)), "euclidean", "shape", [])
    res = detect_anomalies(ci)
    assert res.tx_hashes == []
    assert res.consensus.size == 0
