"""Unit tests for the anomaly-reason attribution (features/explain.py).

Pure: fit a RobustScaler on a tight "normal" population, then check which features
a probe transaction is reported to deviate on.
"""

from __future__ import annotations

import pandas as pd

from app.clustering.model import MODEL_SCHEMA_VERSION, build_shape_model, serialize_model
from app.features.explain import explain_shape_deviations
from app.features.shape import fit_shape_features
from app.service.verdicts import (
    VERDICT_ANOMALY,
    VERDICT_NORMAL,
    _attach_anomaly_reasons,
    top_anomalies_with_verdicts,
)

_COLUMNS = [
    "tx_hash",
    "fees",
    "size",
    "input_count",
    "output_count",
    "total_input_lovelace",
    "total_output_lovelace",
    "net_lovelace",
    "distinct_assets",
    "redeemer_count",
    "hour_of_day",
    "day_of_week",
]


def _row(tx: str, **over: float) -> dict:
    base = {
        "tx_hash": tx,
        "fees": 180_000,
        "size": 400,
        "input_count": 2,
        "output_count": 2,
        "total_input_lovelace": 5_000_000,
        "total_output_lovelace": 4_800_000,
        "net_lovelace": -200_000,
        "distinct_assets": 1,
        "redeemer_count": 1,
        "hour_of_day": 12,
        "day_of_week": 3,
    }
    base.update(over)
    return base


def _scaler():
    """Center/scale fitted on a tight-but-varied normal population (hour ~ midday)."""
    rows = [
        _row(
            f"n{i:02d}",
            input_count=2 + i % 3,
            fees=180_000 + i * 500,
            total_output_lovelace=4_800_000 + i * 1000,
            hour_of_day=11 + i % 3,
        )
        for i in range(20)
    ]
    _, _, _, (center, scale) = fit_shape_features(pd.DataFrame(rows, columns=_COLUMNS))
    return center, scale


def _reasons(center, scale, probe: dict) -> list:
    out = explain_shape_deviations(pd.DataFrame([probe], columns=_COLUMNS), center, scale)
    return out[probe["tx_hash"]]


def test_high_input_count_is_top_reason() -> None:
    center, scale = _scaler()
    reasons = _reasons(center, scale, _row("many_in", input_count=400))
    top = reasons[0]
    assert top.label == "inputs" and top.direction == "high"
    assert "above typical" in top.detail


def test_low_output_value_flagged_low() -> None:
    center, scale = _scaler()
    reasons = _reasons(center, scale, _row("tiny_out", total_output_lovelace=1))
    by_label = {r.label: r for r in reasons}
    assert "output value" in by_label and by_label["output value"].direction == "low"


def test_unusual_time_of_day_combines_cyclical_pair() -> None:
    center, scale = _scaler()
    # Training sits around midday; 3am is on the far side of the clock.
    reasons = _reasons(center, scale, _row("graveyard", hour_of_day=3))
    assert any(r.label == "time of day" and r.direction == "unusual" for r in reasons)


def test_typical_tx_falls_back_to_unusual_combination() -> None:
    center, scale = _scaler()
    [only] = _reasons(center, scale, _row("normal", input_count=3, hour_of_day=12))
    assert only.label == "unusual combination" and only.direction == "combo"


def test_top_k_caps_the_number_of_reasons() -> None:
    center, scale = _scaler()
    # Several features extreme at once; default top_k = 3.
    reasons = _reasons(
        center,
        scale,
        _row(
            "wild",
            input_count=400,
            output_count=400,
            fees=9_000_000,
            total_output_lovelace=1,
            distinct_assets=99,
        ),
    )
    assert len(reasons) == 3


# --- service wiring: _attach_anomaly_reasons -------------------------------


def _model_blob():
    rows = [_row(f"n{i:02d}", input_count=2 + i % 3) for i in range(20)]
    df = pd.DataFrame(rows, columns=_COLUMNS)
    model = build_shape_model(
        train_df=df,
        cluster_of={r["tx_hash"]: 0 for r in rows},
        cluster_verdicts={},
        eps=0.5,
        min_samples=4,
    )
    return serialize_model(model)


class _ReasonRepo:
    """Minimal duck-typed repo exposing only what _attach_anomaly_reasons touches.
    ``model_id`` is unique-able so the global model cache can't mask a bad blob."""

    def __init__(self, blob, probe_df, *, model_id="m1", run_id="r1"):
        self._blob, self._probe, self._mid, self._rid = blob, probe_df, model_id, run_id

    def latest_cluster_model(self, target, feature_set):
        return {"model_id": self._mid, "run_id": self._rid, "blob": self._blob}

    def fetch_shape_features_for(self, target, tx_hashes):
        return self._probe[self._probe["tx_hash"].isin(set(tx_hashes))]


def test_attach_reasons_only_for_anomaly_shape_rows() -> None:
    probe = pd.DataFrame(
        [_row("weird", input_count=400), _row("fine", input_count=3)], columns=_COLUMNS
    )
    rows = [
        {"tx_hash": "weird", "verdict": VERDICT_ANOMALY},
        {"tx_hash": "fine", "verdict": VERDICT_NORMAL},
    ]
    _attach_anomaly_reasons(_ReasonRepo(_model_blob(), probe), "addr", "shape", rows)
    assert rows[0]["reasons"] and rows[0]["reasons"][0]["label"] == "inputs"
    assert "reasons" not in rows[1]  # a normal row is never decorated


def test_attach_reasons_noop_for_graph_feature_set() -> None:
    rows = [{"tx_hash": "x", "verdict": VERDICT_ANOMALY}]
    # Repo must never be consulted for a graph run — pass one that would raise.
    _attach_anomaly_reasons(object(), "addr", "graph", rows)
    assert "reasons" not in rows[0]


def test_attach_reasons_noop_when_no_model() -> None:
    rows = [{"tx_hash": "x", "verdict": VERDICT_ANOMALY}]

    class _NoModelRepo:
        def latest_cluster_model(self, target, feature_set):
            return None

    _attach_anomaly_reasons(_NoModelRepo(), "addr", "shape", rows)
    assert "reasons" not in rows[0]


def test_attach_reasons_uses_current_model_regardless_of_run() -> None:
    # A model fit on a different run than the latest (canonical model vs a later custom
    # re-cluster, or a not-yet-rebuilt model) is the normal state — reasons must still
    # attach against the current model's scaler, not be gated off.
    probe = pd.DataFrame([_row("weird", input_count=400)], columns=_COLUMNS)
    repo = _ReasonRepo(_model_blob(), probe, model_id="rm1", run_id="some-older-run")
    rows = [{"tx_hash": "weird", "verdict": VERDICT_ANOMALY}]
    _attach_anomaly_reasons(repo, "addr", "shape", rows)
    assert rows[0]["reasons"] and rows[0]["reasons"][0]["label"] == "inputs"


def test_attach_reasons_survives_unusable_model_blob() -> None:
    # A stale/pre-signing/garbage blob makes deserialize raise; the read must degrade
    # to no reasons, not 500 (P2). Unique model_id so the cache can't mask it.
    probe = pd.DataFrame([_row("weird", input_count=400)], columns=_COLUMNS)
    repo = _ReasonRepo("not-a-real-blob", probe, model_id="integrity-unique")
    rows = [{"tx_hash": "weird", "verdict": VERDICT_ANOMALY}]
    _attach_anomaly_reasons(repo, "addr", "shape", rows)  # must not raise
    assert "reasons" not in rows[0]


# --- Outliers: reasons only on the latest anomaly run (P3) ------------------


class _OutlierRepo:
    """Full duck-typed repo for top_anomalies_with_verdicts with a real model, so
    reasons can attach. ``latest`` controls which run is the latest anomaly run."""

    def __init__(self, *, latest="ar1"):
        self._latest = latest
        self._blob = _model_blob()
        self._probe = pd.DataFrame(
            [_row("a", input_count=400), _row("lone", input_count=400)], columns=_COLUMNS
        )

    def get_anomaly_run(self, run_id):
        return {
            "run_id": run_id,
            "target": "addr",
            "feature_set": "shape",
            "created_at": "2026-01-01 10:00:00",
        }

    def top_anomalies(self, run_id, target, *, limit, offset=0):
        return [{"tx_hash": "a", "votes": 2}, {"tx_hash": "lone", "votes": 2}]

    def latest_cluster_run(self, target, feature_set, *, near=None):
        return {"run_id": "cr1"}

    def run_tx_labels(self, run_id):
        return {}

    def labels_for_target(self, target):
        return {}

    def cluster_labeled_hashes(self, target):
        return set()

    def latest_anomaly_run(self, target, feature_set, *, near=None):
        return self._latest

    def latest_cluster_model(self, target, feature_set):
        return {
            "model_id": "om1",
            "run_id": "cr1",
            "schema_version": MODEL_SCHEMA_VERSION,
            "blob": self._blob,
        }

    def fetch_shape_features_for(self, target, tx_hashes):
        return self._probe[self._probe["tx_hash"].isin(set(tx_hashes))]


def test_outlier_reasons_present_on_latest_run() -> None:
    out = top_anomalies_with_verdicts(_OutlierRepo(latest="ar1"), "ar1", limit=10)
    flagged = [c for c in out["candidates"] if c["verdict"] == VERDICT_ANOMALY]
    assert flagged and all(c.get("reasons") for c in flagged)


def test_outlier_reasons_suppressed_on_historical_run() -> None:
    # Viewing run "ar1" while the latest is "ar-newer" → omit reasons (their baseline
    # would be the current model, not this run's scaler).
    out = top_anomalies_with_verdicts(_OutlierRepo(latest="ar-newer"), "ar1", limit=10)
    flagged = [c for c in out["candidates"] if c["verdict"] == VERDICT_ANOMALY]
    assert flagged and all("reasons" not in c for c in flagged)
