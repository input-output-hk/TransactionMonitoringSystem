"""Transaction shape/value features.

Monetary and count columns are heavy-tailed, so we apply a signed-log transform
before scaling with a RobustScaler (median/IQR) to keep whales from dominating.
Time-of-day and day-of-week are encoded cyclically (sin/cos).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler

# Columns that are heavy-tailed and benefit from a signed-log transform.
_LOG_COLUMNS = [
    "fees",
    "size",
    "input_count",
    "output_count",
    "total_input_lovelace",
    "total_output_lovelace",
    "net_lovelace",
    "distinct_assets",
    "redeemer_count",
]


def _signed_log1p(x: np.ndarray) -> np.ndarray:
    return np.sign(x) * np.log1p(np.abs(x))


def _cyclical(values: np.ndarray, period: float) -> tuple[np.ndarray, np.ndarray]:
    """Encode a periodic feature as (sin, cos) so it wraps around continuously."""
    angle = 2.0 * np.pi * values / period
    return np.sin(angle), np.cos(angle)


def raw_shape_matrix(df: pd.DataFrame) -> tuple[list[str], np.ndarray, list[str]]:
    """Return ``(tx_hashes, raw, feature_names)`` — the *unscaled* feature matrix.

    Deterministic and stateless (signed-log + cyclical encoding only), so the same
    rows always map to the same raw vectors. The scaling step is separate, which
    lets the fit path fit a scaler and the score path reuse it on new rows.
    """
    df = df.reset_index(drop=True)
    tx_hashes = df["tx_hash"].astype(str).tolist()

    columns: list[np.ndarray] = []
    names: list[str] = []

    for col in _LOG_COLUMNS:
        columns.append(_signed_log1p(df[col].to_numpy(dtype=np.float64)))
        names.append(col)

    # Cyclical hour-of-day (0..23) and day-of-week (ClickHouse 1=Mon..7=Sun).
    hour = df["hour_of_day"].to_numpy(dtype=np.float64)
    dow = (df["day_of_week"].to_numpy(dtype=np.float64) - 1.0) % 7.0
    for prefix, values, period in (("hour", hour, 24.0), ("dow", dow, 7.0)):
        sin, cos = _cyclical(values, period)
        columns.extend((sin, cos))
        names.extend([f"{prefix}_sin", f"{prefix}_cos"])

    return tx_hashes, np.column_stack(columns), names


def build_shape_features(df: pd.DataFrame) -> tuple[list[str], np.ndarray, list[str]]:
    """Return ``(tx_hashes, X, feature_names)``.

    ``X`` is a scaled float64 matrix suitable for Euclidean DBSCAN.
    """
    if df.empty:
        return [], np.empty((0, 0)), []
    tx_hashes, raw, names = raw_shape_matrix(df)
    X = RobustScaler().fit_transform(raw)
    return tx_hashes, X.astype(np.float64), names


def fit_shape_features(
    df: pd.DataFrame,
) -> tuple[list[str], np.ndarray, list[str], tuple[np.ndarray, np.ndarray]]:
    """Like :func:`build_shape_features` but also return the fitted scaler params
    ``(center, scale)`` so new transactions can later be transformed identically
    via :func:`apply_shape_features`."""
    if df.empty:
        return [], np.empty((0, 0)), [], (np.array([]), np.array([]))
    tx_hashes, raw, names = raw_shape_matrix(df)
    scaler = RobustScaler().fit(raw)
    X = scaler.transform(raw).astype(np.float64)
    # sklearn already replaces a zero IQR (constant feature) with 1.0 in scale_;
    # guard explicitly so apply_shape_features' plain (raw-center)/scale can never
    # divide by zero even if that internal behaviour changes.
    scale = np.where(scaler.scale_ == 0.0, 1.0, scaler.scale_)
    return tx_hashes, X, names, (scaler.center_.copy(), scale)


def apply_shape_features(
    df: pd.DataFrame, center: np.ndarray, scale: np.ndarray
) -> tuple[list[str], np.ndarray, list[str]]:
    """Transform new transactions with a previously-fitted scaler (RobustScaler
    semantics: ``(raw - center) / scale``). Mirrors :func:`fit_shape_features`."""
    if df.empty:
        return [], np.empty((0, 0)), []
    tx_hashes, raw, names = raw_shape_matrix(df)
    X = ((raw - center) / scale).astype(np.float64)
    return tx_hashes, X, names
