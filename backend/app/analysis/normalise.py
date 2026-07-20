"""Percentile-based normalisation and baseline resolution.

Implements the scoring framework from the Polimi detection spec (Section 3):
raw feature values are transformed into normalised quantities in [0, 1] using
per-script or per-policy baseline statistics (p50 / p99).  When a script or
policy has fewer than BASELINE_MIN_SAMPLES transactions, the system falls back
to global baselines.
"""

import logging

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


# Minimum (p99 - p50) / p50 spread for a baseline to be usable; tunable via
# the top-level `baselines.min_spread_ratio` block in config/detection.yaml
# (see the rationale comment there). Loaded lazily: scorer_config imports
# this module, so a module-level import here would be circular.
_MIN_BASELINE_SPREAD_RATIO: float | None = None


def _min_baseline_spread_ratio() -> float:
    global _MIN_BASELINE_SPREAD_RATIO
    if _MIN_BASELINE_SPREAD_RATIO is None:
        from app.analysis.scorer_config import baselines_config

        _MIN_BASELINE_SPREAD_RATIO = float(baselines_config()["min_spread_ratio"])
    return _MIN_BASELINE_SPREAD_RATIO


def _baseline_is_usable(row: dict) -> bool:
    """A baseline is usable when (p99 - p50) is at least min_spread_ratio
    of p50.

    Below that spread the per-scope distribution is too tight to
    discriminate; downstream normalisation produces degenerate scores.
    Falling through to the next tier (global / bootstrap) preserves
    intended scorer semantics for that scope.
    """
    p50, p99 = float(row["p50"]), float(row["p99"])
    if p50 <= 0:
        return p99 > 0
    return (p99 - p50) / p50 >= _min_baseline_spread_ratio()


def resolve_baseline(
    feature: str,
    scope_type: str,
    scope_id: str,
    network: str,
    min_samples: int = 0,
    scope_types_allowed: list[str] | None = None,
) -> tuple[float, float, str]:
    """Resolve the (p50, p99) baseline for a feature on a given network, with fallback.

    Tries the requested scope first. Falls back to global within the same
    network when the baseline row does not exist, has fewer than
    ``min_samples`` observations, or has an uninformatively narrow spread
    (see :func:`_baseline_is_usable`).

    ``network`` is required: the baselines table is partitioned by network so
    preprod / preview / mainnet cannot pollute each other.

    ``scope_types_allowed`` optionally restricts which tiers may be consulted.
    When ``None`` (default) the behaviour is unchanged: try ``scope_type`` then
    fall back to ``global``. When a list is given, a tier is only tried if it is
    in the list, so e.g. ``["per_script"]`` resolves per-script then drops
    straight to "missing" (the caller's bootstrap), never consulting global.
    This is required for the multiple_sat extraction axis: the global
    distribution is dominated by legitimate high-volume asset-movers, so a global
    fallback would de-sensitise detection on rare/novel scripts (where one-shot
    double-sat exploits live) instead of leaving them on the conservative
    bootstrap anchor.

    Returns (p50, p99, source) where source is "per_script", "per_policy",
    "global", or "missing".
    """
    if min_samples == 0:
        min_samples = settings.BASELINE_MIN_SAMPLES

    if scope_types_allowed is None or scope_type in scope_types_allowed:
        row = clickhouse.get_baseline(network, scope_type, scope_id, feature)
        if row and row["sample_count"] >= min_samples and _baseline_is_usable(row):
            return row["p50"], row["p99"], scope_type

    # Fallback to global within the same network
    if scope_type != "global" and (scope_types_allowed is None or "global" in scope_types_allowed):
        row = clickhouse.get_baseline(network, "global", "__global__", feature)
        if row and row["sample_count"] >= min_samples and _baseline_is_usable(row):
            return row["p50"], row["p99"], "global"

    # No baseline available — return neutral anchors
    return 0.0, 1.0, "missing"


def resolve_policies_first_seen(network: str, policy_ids: list[str]) -> dict[str, int]:
    """Resolve ``policy_id -> first-seen slot`` for the given policies.

    The scorer-facing entry point for policy-age signals, alongside
    :func:`resolve_baseline`: scorers reach ClickHouse-backed data through
    this resolution layer (which owns caching and the DB dependency) rather
    than importing ``app.db`` directly. Missing policies are omitted from the
    result; callers treat absence as "age unknown".
    """
    return clickhouse.get_policies_first_seen(network, policy_ids)


# Risk-band thresholds, exported so scorers that floor or cap their score to
# a specific band (e.g. multiple_sat's lazy-validator floor, front_running's
# high_band_cap) can key off the same numeric values rather than duplicating
# them in their own configs. Changes here propagate; changes in scorer
# config files do not, so this is the single source of truth.
BAND_CRITICAL_THRESHOLD = 80.0
BAND_HIGH_THRESHOLD = 60.0
BAND_MODERATE_THRESHOLD = 31.0

# Convenience constants: the highest score that still lands in a given band.
# Used by scorers that cap a score at "top of band X" so their band does not
# climb (e.g. multiple_sat's uniform_sweep_guard and token_dust's dos_asset_min
# cap use BAND_MODERATE_MAX; large_value's digits-floor cap uses BAND_LOW_MAX).
# Encapsulates the off-by-one that would otherwise show up at every cap site.
# BAND_LOW_MAX is the top of the bottom (Informational) band; the name is kept
# for the numeric "top of bottom band" role (the client-facing label is set in
# score_to_band).
BAND_MODERATE_MAX = BAND_HIGH_THRESHOLD - 1.0
BAND_LOW_MAX = BAND_MODERATE_THRESHOLD - 1.0

# Top of the Informational band. Scores are floats rounded to 2dp, so the
# Moderate band test is "strictly above 30.0", not ">= 31.0": a 30.5 would
# otherwise fall into a dead zone between the documented 0-30 and 31-59
# bands and silently under-band toward Informational (recall-negative).
BAND_INFORMATIONAL_MAX = BAND_MODERATE_THRESHOLD - 1.0


def score_to_band(score: float) -> str:
    """Map a 0-100 score to the interpretive risk band.

    The bottom band (0-30) is "Informational": no action, the scored-but-not-
    alerting baseline. It was historically labelled "Low", which clients read as
    a low-grade threat; "Informational" reads as "nothing to act on". Renamed
    2026-06; legacy "Low" values are still parsed by RiskBand._missing_.
    """
    if score >= BAND_CRITICAL_THRESHOLD:
        return "Critical"
    if score >= BAND_HIGH_THRESHOLD:
        return "High"
    if score > BAND_INFORMATIONAL_MAX:
        return "Moderate"
    return "Informational"
