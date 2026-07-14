"""Human-readable "why is this an anomaly" attribution for shape features.

The online/batch detectors flag a transaction; this turns that into a short list
of reasons a human can read ("too many inputs", "output value far below typical").

It reuses the exact scaling the detectors see: ``apply_shape_features`` returns
``X[j] = (signed_log1p(raw_j) - center[j]) / scale[j]``, i.e. a robust z-score per
feature (how far from the training median, in IQR-spread units, with a sign). The
features with the largest ``|z|`` are the drivers; the sign gives the direction.

This is a faithful *deviation attribution*, not an exact decomposition of the
IsolationForest/LOF score — those are multivariate and can flag a tx that is
unremarkable on every individual feature but rare in combination. That case is
surfaced honestly as a single "unusual combination" reason.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.features.shape import apply_shape_features

# Below this robust-z (IQR units) a feature is not "standing out". RobustScaler
# scales by the IQR, so |z| >= 2 sits well outside the central 50%.
_Z_THRESHOLD = 2.0
# Magnitude (robust z-units) at which a deviation is described as "far" vs "well"
# above/below typical in the human-readable explanation.
_BAND_FAR_Z = 4.0
_BAND_WELL_Z = 2.75
_TOP_K = 3

# Map the raw model columns to human concepts. The two cyclical pairs collapse to
# one concept each (see _CYCLICAL); everything else is 1:1.
_LABELS: dict[str, str] = {
    "fees": "fee",
    "size": "tx size",
    "input_count": "inputs",
    "output_count": "outputs",
    "total_input_lovelace": "input value",
    "total_output_lovelace": "output value",
    "net_lovelace": "net value",
    "distinct_assets": "assets",
    "redeemer_count": "redeemers",
}
# (sin_col, cos_col) -> concept label, scored as the magnitude of the 2-vector.
_CYCLICAL: list[tuple[str, str, str]] = [
    ("hour_sin", "hour_cos", "time of day"),
    ("dow_sin", "dow_cos", "day of week"),
]


@dataclass(frozen=True)
class Deviation:
    label: str  # human concept, e.g. "inputs"
    direction: str  # "high" | "low" | "unusual" | "combo"
    detail: str  # e.g. "far above typical" / "unusual time of day"


def _band(magnitude: float, *, high: bool) -> str:
    where = "above" if high else "below"
    if magnitude >= _BAND_FAR_Z:
        return f"far {where} typical"
    if magnitude >= _BAND_WELL_Z:
        return f"well {where} typical"
    return f"{where} typical"


# Shown when the row is flagged but no single feature is extreme (the detectors are
# multivariate — they can flag an unusual *combination*).
_COMBINATION = Deviation("unusual combination", "combo", "no single feature is extreme")


def _candidate_deviations(z: np.ndarray, idx: dict[str, int]) -> list[tuple[float, Deviation]]:
    """``(magnitude, Deviation)`` for every concept, unfiltered: each log feature by its
    signed robust-z (direction high/low), each cyclical pair by the magnitude of its
    2-vector (direction "unusual")."""
    # idx.get (not idx[...]) so a future change to the shape feature set degrades to
    # fewer reasons rather than a mid-request KeyError on a renamed/removed column.
    out: list[tuple[float, Deviation]] = []
    for col, label in _LABELS.items():
        i = idx.get(col)
        if i is None:
            continue
        zj = float(z[i])
        mag = abs(zj)
        out.append((mag, Deviation(label, "high" if zj > 0 else "low", _band(mag, high=zj > 0))))
    for sin_col, cos_col, label in _CYCLICAL:
        si, ci = idx.get(sin_col), idx.get(cos_col)
        if si is None or ci is None:
            continue
        mag = float(np.hypot(z[si], z[ci]))
        out.append((mag, Deviation(label, "unusual", f"unusual {label}")))
    return out


def _reasons_for_row(
    z: np.ndarray, idx: dict[str, int], *, top_k: int, z_threshold: float
) -> list[Deviation]:
    """Top deviating concepts for one row's robust-z vector ``z`` (selection policy:
    keep those at/over the threshold, strongest first, capped at ``top_k``)."""
    scored = [c for c in _candidate_deviations(z, idx) if c[0] >= z_threshold]
    scored.sort(key=lambda s: s[0], reverse=True)
    if not scored:
        return [_COMBINATION]
    return [d for _, d in scored[:top_k]]


def explain_shape_deviations(
    raw_df: pd.DataFrame,
    center: np.ndarray,
    scale: np.ndarray,
    *,
    top_k: int = _TOP_K,
    z_threshold: float = _Z_THRESHOLD,
) -> dict[str, list[Deviation]]:
    """Map ``tx_hash -> top reasons`` for the rows in ``raw_df`` (the per-tx shape
    feature frame), scored against a fitted scaler's ``center``/``scale``. Rows whose
    features are all near typical get a single "unusual combination" reason."""
    tx_hashes, X, names = apply_shape_features(raw_df, center, scale)
    if not tx_hashes:
        return {}
    idx = {name: i for i, name in enumerate(names)}
    return {
        h: _reasons_for_row(X[i], idx, top_k=top_k, z_threshold=z_threshold)
        for i, h in enumerate(tx_hashes)
    }
