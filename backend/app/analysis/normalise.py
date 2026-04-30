"""Percentile-based normalisation and baseline resolution.

Implements the scoring framework from the Polimi detection spec (Section 3):
raw feature values are transformed into normalised quantities in [0, 1] using
per-script or per-policy baseline statistics (p50 / p99).  When a script or
policy has fewer than BASELINE_MIN_SAMPLES transactions, the system falls back
to global baselines.
"""

import logging
from typing import Tuple

from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

EPSILON = 1e-6


def normalise(value: float, p50: float, p99: float) -> float:
    """Percentile-based normalisation: clip((value - p50) / (p99 - p50), 0, 1).

    When p99 <= p50 the baseline has zero variance and carries no
    discriminative signal, so we return 0.0. The previous behaviour returned
    1.0 for any value above the constant, which (combined with
    ``normalise_inverted``) caused per-script baselines like p50==p99==10M ADA
    to flag every minUTxO output as maximally suspicious.
    """
    denom = p99 - p50
    if denom <= 0:
        return 0.0
    return max(0.0, min(1.0, (value - p50) / (denom + EPSILON)))


def normalise_inverted(value: float, p50: float, p99: float) -> float:
    """Inverted normalisation: lower values produce higher scores.

    Used for features like lovelace_amount in dust attacks (low ADA = suspicious)
    and redeemer_input_ratio (ratio near 0 = suspicious).

    Degenerate baselines (p99 <= p50) return 0.0 rather than ``1 - 0 = 1``:
    a zero-variance baseline cannot tell a "low" value apart from a "normal"
    one, so the inverted axis must not push toward Critical.
    """
    if p99 - p50 <= 0:
        return 0.0
    return 1.0 - normalise(value, p50, p99)


def resolve_baseline(
    feature: str,
    scope_type: str,
    scope_id: str,
    network: str,
    min_samples: int = 0,
) -> Tuple[float, float, str]:
    """Resolve the (p50, p99) baseline for a feature on a given network, with fallback.

    Tries the requested scope first. If sample_count < min_samples (or the
    baseline row does not exist), falls back to global within the same network.

    ``network`` is required: the baselines table is partitioned by network so
    preprod / preview / mainnet cannot pollute each other.

    Returns (p50, p99, source) where source is "per_script", "per_policy",
    "global", or "missing".
    """
    if min_samples == 0:
        min_samples = settings.BASELINE_MIN_SAMPLES

    row = clickhouse.get_baseline(network, scope_type, scope_id, feature)
    if row and row["sample_count"] >= min_samples:
        return row["p50"], row["p99"], scope_type

    # Fallback to global within the same network
    if scope_type != "global":
        row = clickhouse.get_baseline(network, "global", "__global__", feature)
        if row and row["sample_count"] >= min_samples:
            return row["p50"], row["p99"], "global"

    # No baseline available — return neutral anchors
    return 0.0, 1.0, "missing"


# Risk-band thresholds, exported so scorers that floor or cap their score to
# a specific band (e.g. multiple_sat's lazy-validator floor, front_running's
# high_band_cap) can key off the same numeric values rather than duplicating
# them in their own configs. Changes here propagate; changes in scorer
# config files do not, so this is the single source of truth.
BAND_CRITICAL_THRESHOLD = 80.0
BAND_HIGH_THRESHOLD = 60.0
BAND_MODERATE_THRESHOLD = 31.0


def score_to_band(score: float) -> str:
    """Map a 0-100 score to the interpretive risk band."""
    if score >= BAND_CRITICAL_THRESHOLD:
        return "Critical"
    if score >= BAND_HIGH_THRESHOLD:
        return "High"
    if score >= BAND_MODERATE_THRESHOLD:
        return "Moderate"
    return "Low"
