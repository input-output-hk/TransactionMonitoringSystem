"""Circular Transfers attack scorer (Class 7).

Detects value cycles in the transfer graph: ADA or native assets travel
through a sequence of addresses and return to the origin (or same cluster)
with near-zero net displacement (fee-only loss).  Motivations include wash
trading, AML layering, and clustering heuristic confusion.

This scorer operates on **cycle records** populated by a graph analysis
pipeline.  When no cycle data is available for a transaction, the gate
returns False.

Sub-scores (Polimi Section 4.7.3):
  amount_similarity    (0.30): preservation of value across hops, fixed anchors
  cycle_recurrence     (0.30): per-cluster baseline
  recipient_entropy    (0.20): inverted, fixed anchors
  auxiliary            (0.10): round amounts + temporal concentration
  speed                (0.10): inter-hop slot delta reciprocal, fixed anchors

Infrastructure dependency: transfer graph construction and cycle detection
(bounded 6-hop BFS from tx sender) must be run by a background analysis
task.  Until that infrastructure is built, this scorer's gate will not pass.
"""

import logging
from typing import Any, Dict, Optional

from app.analysis.normalise import normalise, normalise_inverted, resolve_baseline
from app.analysis.scorers.base import BaseScorer, ScorerResult

logger = logging.getLogger(__name__)

# Fixed anchors (Polimi Section 5.4)
_AMOUNT_SIM_P50 = 0.70
_AMOUNT_SIM_P99 = 0.97
_ENTROPY_P50 = 0.80     # high entropy = normal (many distinct recipients)
_ENTROPY_P99 = 0.30     # low entropy = suspicious (same addresses recycled)
_HOP_DELTA_INV_P50 = 1 / 20   # 20 slots between hops = normal
_HOP_DELTA_INV_P99 = 1 / 2    # 2 slots = automation-consistent

# Fee tolerance multiplier for net_loss_ratio gate (relaxed to catch
# cycles with moderate value loss; strict threshold used for score capping)
FEE_TOLERANCE_MULTIPLIER = 4.0
FEE_TOLERANCE_STRICT = 2.0

EPSILON = 1e-6


def _get_cycle_data(features: Dict[str, Any]) -> Optional[Dict]:
    """Extract cycle detection data from features if available.

    The engine populates features["cycle"] when a tx is part of a detected
    transfer cycle.  Structure:
      {
        "cycle_length": int,
        "addresses": list[str],
        "amount_similarity": float,
        "net_loss_ratio": float,
        "recurrence_count": int,
        "recipient_entropy": float,
        "round_amount_flag": bool,
        "temporal_concentration": float,
        "mean_inter_hop_delta_slots": float,
        "origin_cluster": str,
      }
    """
    return features.get("cycle")


def _estimate_fee_ratio(cycle_length: int) -> float:
    """Estimate expected fee-only loss ratio for a cycle of k hops.

    Each hop costs roughly 0.17-0.30 ADA in fees.  For a cycle transferring
    ~10 ADA through k hops, the fee ratio is approximately k * 0.2 / 10.
    """
    return cycle_length * 0.02  # ~2% per hop as rough estimate


class CircularScorer(BaseScorer):
    name = "circular"

    def gate(self, features: Dict[str, Any]) -> bool:
        """Transaction must be part of a detected transfer cycle."""
        cycle = _get_cycle_data(features)
        if not cycle:
            return False

        length = cycle.get("cycle_length", 0)
        if not (2 <= length <= 6):
            return False

        # Net loss must be consistent with fee-only loss
        net_loss = cycle.get("net_loss_ratio", 1.0)
        expected = _estimate_fee_ratio(length)
        if net_loss > expected * FEE_TOLERANCE_MULTIPLIER:
            return False

        return True

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        cycle = _get_cycle_data(features)
        if not cycle:
            return ScorerResult(score=0.0)

        origin = cycle.get("origin_cluster", "__unknown__")

        # Sub-score 1: amount_similarity (weight = 0.30)
        amt_sim = cycle.get("amount_similarity", 0.0)
        s_amount = normalise(amt_sim, p50=_AMOUNT_SIM_P50, p99=_AMOUNT_SIM_P99)

        # Sub-score 2: cycle_recurrence (weight = 0.30)
        recurrence = cycle.get("recurrence_count", 0)
        p50_r, p99_r, bl1 = resolve_baseline(
            "cycle_recurrence", "per_cluster", origin,
        )
        if bl1 == "missing":
            p50_r, p99_r = 0.0, 5.0  # bootstrap
        s_recurrence = normalise(recurrence, p50=p50_r, p99=p99_r)

        # Sub-score 3: recipient_entropy inverted (weight = 0.20)
        entropy = cycle.get("recipient_entropy", 1.0)
        # Inverted: low entropy (same addresses) -> high score
        # The fixed anchors are inverted: p50=0.80 (normal), p99=0.30 (suspicious)
        # We normalise (1 - entropy) against (1 - 0.80, 1 - 0.30) = (0.20, 0.70)
        s_entropy = normalise(1.0 - entropy, p50=1.0 - _ENTROPY_P50, p99=1.0 - _ENTROPY_P99)

        # Sub-score 4: auxiliary: round amounts + temporal concentration (weight = 0.10)
        s_round = 1.0 if cycle.get("round_amount_flag") else 0.0
        temporal = cycle.get("temporal_concentration", 0.0)
        s_timing = normalise(temporal, p50=0.30, p99=0.85)
        s_auxiliary = (s_round + s_timing) / 2.0

        # Sub-score 5: inter-hop speed (weight = 0.10)
        hop_delta = max(cycle.get("mean_inter_hop_delta_slots", 100), EPSILON)
        hop_inv = 1.0 / hop_delta
        s_speed = normalise(hop_inv, p50=_HOP_DELTA_INV_P50, p99=_HOP_DELTA_INV_P99)

        bl_source = bl1 if bl1 != "missing" else "bootstrap"

        raw = (
            0.30 * s_amount
            + 0.30 * s_recurrence
            + 0.20 * s_entropy
            + 0.10 * s_auxiliary
            + 0.10 * s_speed
        )
        final = round(max(0.0, min(1.0, raw)) * 100, 2)

        # Fee-ratio cap: if net loss exceeds the strict fee-only threshold,
        # the cycle may be incidental transfers rather than deliberate
        # layering.  Cap at Moderate band (max 59).
        net_loss = cycle.get("net_loss_ratio", 0.0)
        expected_fee = _estimate_fee_ratio(cycle.get("cycle_length", 2))
        if net_loss > expected_fee * FEE_TOLERANCE_STRICT and final > 59:
            final = 59.0

        reasons = []
        if s_amount > 0.5:
            reasons.append("high_amount_preservation")
        if s_recurrence > 0.5:
            reasons.append("repeated_cycle_pattern")
        if s_entropy > 0.5:
            reasons.append("low_recipient_diversity")
        if s_speed > 0.5:
            reasons.append("rapid_inter_hop_timing")

        return ScorerResult(
            score=final,
            sub_scores={
                "amount_similarity": round(s_amount, 4),
                "cycle_recurrence": round(s_recurrence, 4),
                "recipient_entropy_inv": round(s_entropy, 4),
                "auxiliary": round(s_auxiliary, 4),
                "speed": round(s_speed, 4),
                "cycle_length": cycle.get("cycle_length", 0),
                "net_loss_ratio": round(cycle.get("net_loss_ratio", 0), 4),
            },
            reasons=reasons,
            baseline_source=bl_source,
        )
