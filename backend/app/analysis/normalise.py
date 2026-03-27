"""Percentile-based normalisation and baseline resolution.

Implements the scoring framework from the Polimi detection spec (Section 3):
raw feature values are transformed into normalised quantities in [0, 1] using
per-script or per-policy baseline statistics (p50 / p99).  When a script or
policy has fewer than BASELINE_MIN_SAMPLES transactions, the system falls back
to global baselines.
"""

import logging
from typing import Optional, Tuple

from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

EPSILON = 1e-6


def normalise(value: float, p50: float, p99: float) -> float:
    """Percentile-based normalisation: clip((value - p50) / (p99 - p50), 0, 1).

    When p99 <= p50 (degenerate baseline), returns 0 if value <= p50, else 1.
    """
    denom = p99 - p50
    if denom <= 0:
        return 0.0 if value <= p50 else 1.0
    return max(0.0, min(1.0, (value - p50) / (denom + EPSILON)))


def normalise_inverted(value: float, p50: float, p99: float) -> float:
    """Inverted normalisation: lower values produce higher scores.

    Used for features like lovelace_amount in dust attacks (low ADA = suspicious)
    and redeemer_input_ratio (ratio near 0 = suspicious).
    """
    return 1.0 - normalise(value, p50, p99)


def resolve_baseline(
    feature: str,
    scope_type: str = "global",
    scope_id: str = "__global__",
    min_samples: int = 0,
) -> Tuple[float, float, str]:
    """Resolve the (p50, p99) baseline for a feature, with fallback.

    Tries the requested scope first.  If sample_count < min_samples (or the
    baseline row does not exist), falls back to global.

    Returns (p50, p99, source) where source is "per_script", "per_policy",
    "global", or "missing".
    """
    if min_samples == 0:
        min_samples = settings.BASELINE_MIN_SAMPLES

    row = clickhouse.get_baseline(scope_type, scope_id, feature)
    if row and row["sample_count"] >= min_samples:
        return row["p50"], row["p99"], scope_type

    # Fallback to global
    if scope_type != "global":
        row = clickhouse.get_baseline("global", "__global__", feature)
        if row and row["sample_count"] >= min_samples:
            return row["p50"], row["p99"], "global"

    # No baseline available — return neutral anchors
    return 0.0, 1.0, "missing"


def resolve_baseline_or_fixed(
    feature: str,
    fixed_p50: Optional[float] = None,
    fixed_p99: Optional[float] = None,
    scope_type: str = "global",
    scope_id: str = "__global__",
) -> Tuple[float, float, str]:
    """Resolve baseline with fixed-anchor fallback.

    Used for dimensionless derived features (datum_ratio, redeemer_input_ratio)
    where the Polimi spec defines fixed anchors rather than per-script baselines.
    """
    # Try dynamic baseline first; fall back to fixed anchors
    p50, p99, source = resolve_baseline(feature, scope_type, scope_id)
    if source != "missing":
        return p50, p99, source

    if fixed_p50 is not None and fixed_p99 is not None:
        return fixed_p50, fixed_p99, "fixed"

    return p50, p99, source


def score_to_band(score: float) -> str:
    """Map a 0-100 score to the interpretive risk band."""
    if score >= 80:
        return "Critical"
    if score >= 60:
        return "High"
    if score >= 31:
        return "Moderate"
    return "Low"
