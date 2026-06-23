"""Unit tests for the online classification model (clustering/model.py).

Pure sklearn on tiny synthetic features — no ClickHouse, no network. Verifies the
fit → serialize → score round-trip, nearest-centroid assignment, anomaly voting,
and verdict inheritance.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.clustering.model import (
    _verdict,
    build_shape_model,
    deserialize_model,
    load_cluster_model,
    score_shape,
    serialize_model,
)

_SHAPE_COLUMNS = [
    "tx_hash", "fees", "size", "input_count", "output_count",
    "total_input_lovelace", "total_output_lovelace", "net_lovelace",
    "distinct_assets", "redeemer_count", "hour_of_day", "day_of_week",
]


def _row(tx: str, scale: float, *, hour: int = 12, dow: int = 3) -> dict:
    """A shape-feature row whose magnitude is controlled by `scale`, so two
    well-separated `scale` bands form two clean clusters."""
    return {
        "tx_hash": tx,
        "fees": 150_000 + scale * 1000,
        "size": 300 + scale,
        "input_count": 1 + int(scale % 3),
        "output_count": 2 + int(scale % 2),
        "total_input_lovelace": int(1_000_000 * scale),
        "total_output_lovelace": int(990_000 * scale),
        "net_lovelace": -int(10_000 * scale),
        "distinct_assets": int(scale % 4),
        "redeemer_count": 1,
        "hour_of_day": hour,
        "day_of_week": dow,
    }


def _train_frame() -> tuple[pd.DataFrame, dict[str, int]]:
    """Two tight clusters (low vs high magnitude), 8 txs each."""
    rows: list[dict] = []
    cluster_of: dict[str, int] = {}
    for i in range(8):
        h = f"lo{i:02d}".ljust(64, "0")
        rows.append(_row(h, scale=1.0 + i * 0.02))
        cluster_of[h] = 0
    for i in range(8):
        h = f"hi{i:02d}".ljust(64, "0")
        rows.append(_row(h, scale=50.0 + i * 0.02))
        cluster_of[h] = 1
    return pd.DataFrame(rows, columns=_SHAPE_COLUMNS), cluster_of


def test_build_model_captures_clusters_and_scaler() -> None:
    df, cluster_of = _train_frame()
    model = build_shape_model(
        train_df=df, cluster_of=cluster_of, cluster_verdicts={}, eps=0.5, min_samples=4
    )
    assert model.n_clusters == 2
    assert sorted(model.cluster_ids) == [0, 1]
    assert model.centroids.shape[0] == 2
    assert model.iso_model is not None and model.lof_model is not None
    # Scaler params have one entry per feature column.
    assert model.center.shape[0] == model.centroids.shape[1]


def test_score_assigns_near_point_and_flags_outlier() -> None:
    df, cluster_of = _train_frame()
    model = build_shape_model(
        train_df=df, cluster_of=cluster_of, cluster_verdicts={}, eps=0.5, min_samples=4
    )
    probe = pd.DataFrame(
        [
            _row("near".ljust(64, "0"), scale=1.05),  # squarely in cluster 0
            _row("out".ljust(64, "0"), scale=5000.0),  # far outside both clusters
        ],
        columns=_SHAPE_COLUMNS,
    )
    by_hash = {c.tx_hash: c for c in score_shape(model, probe)}

    near = by_hash["near".ljust(64, "0")]
    assert near.cluster_id == 0
    assert near.verdict == "normal"

    out = by_hash["out".ljust(64, "0")]
    assert out.cluster_id == -1  # unassigned (online noise)
    assert out.votes >= 1
    assert out.verdict == "anomaly"


def test_online_verdict_requires_all_detectors_to_agree() -> None:
    """The collinear cluster-noise flag must not carry a flag with a single
    detector: with both detectors fit, auto-anomaly needs votes == 2. (Regression
    for the false-positive fix — see _verdict.)"""
    df, cluster_of = _train_frame()
    model = build_shape_model(
        train_df=df, cluster_of=cluster_of, cluster_verdicts={}, eps=0.5, min_samples=4
    )
    assert model.iso_model is not None and model.lof_model is not None  # n_detectors == 2

    # cluster_id -1 (online noise) on its own no longer drives the verdict.
    assert _verdict(model, -1, votes=0, n_detectors=2) == "normal"
    assert _verdict(model, -1, votes=1, n_detectors=2) == "normal"  # one detector: not enough
    assert _verdict(model, -1, votes=2, n_detectors=2) == "anomaly"  # both agree

    # A single-detector model flags when that one detector fires.
    assert _verdict(model, -1, votes=1, n_detectors=1) == "anomaly"
    # A model with no detectors never auto-flags.
    assert _verdict(model, -1, votes=0, n_detectors=0) == "normal"


def test_score_votes_exclude_noise_and_stay_in_detector_range() -> None:
    df, cluster_of = _train_frame()
    model = build_shape_model(
        train_df=df, cluster_of=cluster_of, cluster_verdicts={}, eps=0.5, min_samples=4
    )
    probe = pd.DataFrame(
        [
            _row("near".ljust(64, "0"), scale=1.05),
            _row("out".ljust(64, "0"), scale=5000.0),
        ],
        columns=_SHAPE_COLUMNS,
    )
    # votes counts independent detectors only (0..2), never the noise pseudo-vote.
    assert all(0 <= c.votes <= 2 for c in score_shape(model, probe))


def test_online_noise_below_auto_anomaly_bar_is_still_surfaced_for_review() -> None:
    """Recall guard for the precision-tightening in _verdict (recall-first).

    The online path raises the *auto-anomaly* bar to unanimous detectors, so a tx
    that trips fewer than n_detectors resolves to ``normal`` and does not
    auto-publish. That tightening is only safe because such a tx is NOT dropped:
    an out-of-distribution point still lands in ``cluster_id == -1`` and ranks
    ABOVE an in-distribution normal by ``consensus``, so an analyst review/sort
    still surfaces it. This pins that the signal is never silently lost; if a
    future change makes a sub-threshold OOD point indistinguishable from a benign
    in-cluster one, this fails."""
    df, cluster_of = _train_frame()
    model = build_shape_model(
        train_df=df, cluster_of=cluster_of, cluster_verdicts={}, eps=0.5, min_samples=4
    )
    probe = pd.DataFrame(
        [
            _row("near".ljust(64, "0"), scale=1.05),   # in-distribution, in cluster 0
            _row("out".ljust(64, "0"), scale=5000.0),  # far outside every cluster
        ],
        columns=_SHAPE_COLUMNS,
    )
    by_hash = {c.tx_hash: c for c in score_shape(model, probe)}
    near = by_hash["near".ljust(64, "0")]
    out = by_hash["out".ljust(64, "0")]

    # A sub-threshold OOD tx (verdict "normal", votes < n_detectors) is still
    # surfaced: unassigned to any frozen cluster and ranked above the in-cluster
    # normal by consensus, so it cannot hide among benign traffic.
    assert _verdict(model, -1, votes=1, n_detectors=2) == "normal"  # the tightening
    assert out.cluster_id == -1
    assert out.consensus > near.consensus


def test_verdict_inherits_cluster_label() -> None:
    df, cluster_of = _train_frame()
    model = build_shape_model(
        train_df=df,
        cluster_of=cluster_of,
        cluster_verdicts={0: "malicious"},
        eps=0.5,
        min_samples=4,
    )
    probe = pd.DataFrame([_row("near".ljust(64, "0"), scale=1.05)], columns=_SHAPE_COLUMNS)
    [c] = score_shape(model, probe)
    assert c.cluster_id == 0
    assert c.verdict == "malicious"  # inherited from the cluster


def test_serialize_round_trip_preserves_scoring() -> None:
    df, cluster_of = _train_frame()
    model = build_shape_model(
        train_df=df, cluster_of=cluster_of, cluster_verdicts={}, eps=0.5, min_samples=4
    )
    probe = pd.DataFrame(
        [_row("near".ljust(64, "0"), scale=1.1), _row("out".ljust(64, "0"), scale=9000.0)],
        columns=_SHAPE_COLUMNS,
    )
    before = score_shape(model, probe)
    after = score_shape(deserialize_model(serialize_model(model)), probe)
    assert [c.cluster_id for c in before] == [c.cluster_id for c in after]
    assert [c.verdict for c in before] == [c.verdict for c in after]
    assert np.allclose(
        [c.consensus for c in before], [c.consensus for c in after], equal_nan=True
    )


def test_load_cluster_model_caches_by_id() -> None:
    df, cluster_of = _train_frame()
    model = build_shape_model(
        train_df=df, cluster_of=cluster_of, cluster_verdicts={}, eps=0.5, min_samples=4
    )
    row = {"model_id": "cache-test-001", "blob": serialize_model(model)}
    first = load_cluster_model(row)
    # Same id, deliberately corrupt blob: a cache hit must NOT touch it (else it would
    # raise on deserialize). Proves the second call is served from cache.
    second = load_cluster_model({"model_id": "cache-test-001", "blob": "not-a-real-blob"})
    assert first is second


def test_empty_probe_returns_no_classifications() -> None:
    df, cluster_of = _train_frame()
    model = build_shape_model(
        train_df=df, cluster_of=cluster_of, cluster_verdicts={}, eps=0.5, min_samples=4
    )
    assert score_shape(model, pd.DataFrame(columns=_SHAPE_COLUMNS)) == []
