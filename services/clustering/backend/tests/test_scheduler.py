"""The feed scheduler's per-contract decision (``_decide``).

These lock the Phase-1 fix for the non-convergent auto-refit loop: an
un-clusterable fit (coverage below ``min_cluster_coverage``) must NOT be re-fit on
drift (a re-fit reproduces the same majority-noise model), and no contract may be
auto-re-fit more than once per ``feed_refit_min_interval_seconds``. Recall is
untouched here: whichever branch is chosen, ``classify`` (which scores + publishes
every tick) still runs; only the futile ``onboard`` re-fit is skipped.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.service.scheduler import _decide

_NOW = 1_000_000_000  # fixed clock; tests offset last_fit_at relative to it


def _contract(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "target": "addr1a",
        "target_type": "address",
        "status": "done",
        "drift_score": 0.0,
        "fit_coverage": 0.9,  # clusterable by default
        "last_fit_at": 0,  # never auto-re-fit yet
    }
    row.update(over)
    return row


def _high_drift() -> float:
    # Comfortably over recluster_noise_threshold whatever it is configured to.
    return min(1.0, get_settings().recluster_noise_threshold + 0.5)


def _unclusterable_cov() -> float:
    # Below min_cluster_coverage but above the -1 "unknown" sentinel.
    return max(0.0, get_settings().min_cluster_coverage - 0.1)


def _stale() -> int:
    # last_fit_at old enough that the anti-flap interval has elapsed.
    return _NOW - (get_settings().feed_refit_min_interval_seconds + 1)


def _recent() -> int:
    return _NOW - 1  # inside the anti-flap interval


def test_pending_contract_onboards() -> None:
    assert _decide(_contract(status="pending"), get_settings(), now=_NOW) == ("onboard", 1)


def test_processing_and_failed_are_left_alone() -> None:
    assert _decide(_contract(status="processing"), get_settings(), now=_NOW) is None
    assert _decide(_contract(status="failed"), get_settings(), now=_NOW) is None


def test_clusterable_high_drift_refits_when_not_throttled() -> None:
    """A genuinely-drifted, clusterable model re-fits (converges) exactly as before."""
    c = _contract(drift_score=_high_drift(), fit_coverage=0.9, last_fit_at=_stale())
    assert _decide(c, get_settings(), now=_NOW) == ("onboard", 1)


def test_clusterable_high_drift_throttled_classifies() -> None:
    """Anti-flap: a second re-fit inside the interval is collapsed to classify."""
    c = _contract(drift_score=_high_drift(), fit_coverage=0.9, last_fit_at=_recent())
    assert _decide(c, get_settings(), now=_NOW) == ("classify", 0)


def test_unclusterable_high_drift_does_not_loop() -> None:
    """THE loop fix: un-clusterable + high drift within the interval -> classify,
    never a futile re-fit."""
    c = _contract(
        drift_score=_high_drift(), fit_coverage=_unclusterable_cov(), last_fit_at=_recent()
    )
    assert _decide(c, get_settings(), now=_NOW) == ("classify", 0)


def test_unclusterable_rebaselines_on_slow_cadence() -> None:
    """An un-clusterable contract still re-fits once per slow interval to keep the
    detector baselines (RobustScaler + iso/LOF thresholds) fresh."""
    c = _contract(
        drift_score=_high_drift(), fit_coverage=_unclusterable_cov(), last_fit_at=_stale()
    )
    assert _decide(c, get_settings(), now=_NOW) == ("onboard", 1)


def test_unclusterable_low_drift_classifies() -> None:
    c = _contract(drift_score=0.0, fit_coverage=_unclusterable_cov(), last_fit_at=_recent())
    assert _decide(c, get_settings(), now=_NOW) == ("classify", 0)


def test_legacy_unknown_coverage_behaves_as_before() -> None:
    """A pre-011 row (fit_coverage -1, last_fit_at 0) with high drift re-fits, so the
    deploy changes nothing until the first fit records a real coverage (self-heal)."""
    c = _contract(drift_score=_high_drift(), fit_coverage=-1.0, last_fit_at=0)
    assert _decide(c, get_settings(), now=_NOW) == ("onboard", 1)
