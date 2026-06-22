"""Model-blob signing: HMAC verification must run BEFORE joblib (pickle) load."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from app.clustering.model import (
    ModelIntegrityError,
    ShapeModel,
    deserialize_model,
    serialize_model,
)
from app.config import get_settings


def _model() -> ShapeModel:
    return ShapeModel(
        feature_names=["f1"],
        center=np.array([0.0]),
        scale=np.array([1.0]),
        cluster_ids=[0],
        centroids=np.array([[0.0]]),
        radii=np.array([1.0]),
        cluster_verdicts={},
        eps=0.5,
        min_samples=4,
        iso_threshold=float("nan"),
        lof_threshold=float("nan"),
        iso_bounds=(float("nan"), float("nan")),
        lof_bounds=(float("nan"), float("nan")),
    )


def _set_keys(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MODEL_SIGNING_KEYS", value)
    get_settings.cache_clear()


def test_unsigned_roundtrip_when_no_keys() -> None:
    blob = serialize_model(_model())
    assert blob.startswith("tms-model:1:unsigned:")
    assert deserialize_model(blob).eps == 0.5


def test_signed_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_keys(monkeypatch, "k1")
    blob = serialize_model(_model())
    assert ":unsigned:" not in blob
    assert deserialize_model(blob).eps == 0.5


def test_tampered_payload_rejected_before_unpickling(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_keys(monkeypatch, "k1")
    blob = serialize_model(_model())
    prefix, b64 = blob.rsplit(":", 1)
    tampered = prefix + ":" + ("A" + b64[1:] if b64[0] != "A" else "B" + b64[1:])
    with pytest.raises(ModelIntegrityError, match="HMAC"):
        deserialize_model(tampered)


def test_key_rotation_verifies_with_any_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_keys(monkeypatch, "old-key")
    blob = serialize_model(_model())
    _set_keys(monkeypatch, "new-key,old-key")  # sign with first, verify with any
    assert deserialize_model(blob).eps == 0.5


def test_legacy_unprefixed_blob_rejected_never_fallback_loaded() -> None:
    with pytest.raises(ModelIntegrityError, match="predates"):
        deserialize_model("bm90LWEtcmVhbC1ibG9i")  # pre-signing base64 format


def test_unsigned_blob_rejected_once_keys_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob = serialize_model(_model())  # unsigned (no keys yet)
    _set_keys(monkeypatch, "k1")
    with pytest.raises(ModelIntegrityError):
        deserialize_model(blob)


# --- Batch vs online voting semantics (pinned; see docs/algorithms.md) -------


def test_online_thresholds_are_frozen_training_values_not_ranks() -> None:
    """Online voting applies the TRAINING set's value threshold — it must NOT be
    re-derived from the incoming batch (rank semantics). Pinned so a future
    "alignment" of batch and online voting trips this test and gets re-litigated
    against docs/algorithms.md."""
    import pandas as pd

    from app.clustering.model import build_shape_model, score_shape

    # 56 tight normals (deterministic jitter, no monotone ramp) + 4 clear outliers.
    # Both numbers are load-bearing for cross-arch stability (x86 vs arm BLAS):
    #  * n must comfortably exceed LOF_NEIGHBORS (20) so each normal's k-NN is
    #    all-normal and its LOF sits at ~1.0 while the outliers' LOF is >> 1 — at
    #    n=20, k=n-1 makes every LOF score collapse to exactly 1.0 and the vote
    #    rides on float noise;
    #  * with top_quantile=0.05 the frozen thresholds then land AMONG the outlier
    #    scores, far above every normal's, so no >= comparison is marginal.
    def _normal(i: int) -> dict[str, Any]:
        return {
            "tx_hash": f"{i:064x}",
            "fees": 150_000 + (i * 37) % 1_100, "size": 300 + (i * 17) % 13,
            "input_count": 1, "output_count": 2,
            "total_input_lovelace": 1_000_000 + (i * 53) % 9_000,
            "total_output_lovelace": 990_000 + (i * 29) % 8_000,
            "net_lovelace": -10_000 - (i * 13) % 900, "distinct_assets": 0,
            "redeemer_count": 1, "hour_of_day": 12, "day_of_week": 3,
        }

    def _outlier(j: int) -> dict[str, Any]:
        return {
            **_normal(90 + j), "tx_hash": f"{'fed':>03}{j:061x}"[-64:],
            "fees": 60_000_000 + j * 5_000_000, "size": 14_000 + j * 500,
            "input_count": 25 + j, "output_count": 40 + j,
            "total_input_lovelace": 10**12, "total_output_lovelace": 10**12,
            "net_lovelace": -5 * 10**9, "distinct_assets": 30, "redeemer_count": 8,
        }

    rows = [_normal(i) for i in range(56)] + [_outlier(j) for j in range(4)]
    train = pd.DataFrame(rows)
    model = build_shape_model(
        train_df=train, cluster_of={r["tx_hash"]: 0 for r in rows},
        cluster_verdicts={}, eps=0.5, min_samples=4,
    )
    # Frozen value thresholds exist and came from the training distribution.
    assert np.isfinite(model.iso_threshold) and np.isfinite(model.lof_threshold)

    # A batch of three mid-distribution normals: under rank semantics the
    # relative-max would always flag; under frozen-value semantics none should.
    normals = pd.DataFrame(rows[9:12])
    votes = [c.votes for c in score_shape(model, normals)]
    assert all(v == 0 for v in votes)

    # A far-out-of-distribution tx must flag on values alone. It has to dwarf the
    # TRAINING outliers on their own axes — the frozen thresholds are anchored to
    # their scores, so a probe milder than them scores as in-distribution.
    extreme = pd.DataFrame([{
        **rows[0], "tx_hash": "f" * 64, "fees": 10**10, "size": 10**7,
        "input_count": 500, "output_count": 800,
        "total_input_lovelace": 10**14, "total_output_lovelace": 10**14,
        "net_lovelace": -(10**13), "distinct_assets": 300, "redeemer_count": 60,
    }])
    assert score_shape(model, extreme)[0].votes >= 1
