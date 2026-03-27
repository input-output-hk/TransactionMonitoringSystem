"""Front-Running attack scorer (Class 5).

Detects mempool collision pairs: two transactions sharing at least one input,
where one consistently wins on-chain.  The key signals are temporal proximity
in the mempool and attacker recurrence.

This scorer operates on **transaction pairs** identified by the mempool
collision tracker.  When no collision data is available for a transaction,
the gate returns False.

Sub-scores (Polimi Section 4.5.3):
  collision_outcome    (0.35): confirmed front-run vs ambiguous vs no front-run
  mempool_delta_ms     (0.30): reciprocal of time delta, fixed anchors
  attacker_recurrence  (0.25): per-cluster baseline
  structural_similarity (0.10): fee/TTL/change address similarity

Infrastructure dependency: mempool collision tracking (PostgreSQL table
mempool_collisions) must be populated by the ingestion layer's mempool loop.
Until that infrastructure is built, this scorer's gate will not pass.
"""

import logging
from typing import Any, Dict, Optional

from app.analysis.normalise import normalise, resolve_baseline
from app.analysis.scorers.base import BaseScorer, ScorerResult

logger = logging.getLogger(__name__)

# Fixed anchors (Polimi Section 5.4)
_DELTA_INV_P50 = 1 / 2000    # 2000ms = normal propagation variance
_DELTA_INV_P99 = 1 / 200     # 200ms = automation-consistent
_FEE_DELTA_P50 = 500         # 500 lovelace fee difference = normal variance
_FEE_DELTA_P99 = 5000        # 5000 lovelace = near-identical fees suggest mimicry
_TTL_DELTA_P50 = 10          # 10 slots TTL difference = normal
_TTL_DELTA_P99 = 100         # 100 slots = structurally similar TTLs

# Outcome score mapping
# TX_B_CONFIRMED means the later-seen tx won: strong front-running signal
# TX_A_CONFIRMED means the earlier-seen tx won: no front-run
_OUTCOME_SCORES = {
    "TX_B_CONFIRMED": 1.0,   # later tx confirmed, earlier tx lost UTxO
    "TX1_FAILS_UTXO_SPENT": 1.0,  # legacy compat
    "BOTH_PENDING": 0.5,
    "TX_A_CONFIRMED": 0.0,   # earlier tx won, no front-run
    "TX1_WINS": 0.0,         # legacy compat
    "TX2_WINS": 0.3,         # legacy compat
}

EPSILON = 1e-6


def _get_collision_data(features: Dict[str, Any]) -> Optional[Dict]:
    """Extract mempool collision data from features if available.

    The engine populates features["collision"] when a tx appears in the
    mempool_collisions table.  Structure:
      {
        "counterpart_tx": str,
        "shared_inputs": int,
        "delta_ms": float,
        "outcome": str,
        "counterpart_fee": int,
        "counterpart_ttl": int,
        "shares_change_address": bool,
        "attacker_win_count": int,
      }
    """
    return features.get("collision")


class FrontRunningScorer(BaseScorer):
    name = "front_running"

    def gate(self, features: Dict[str, Any]) -> bool:
        """Transaction must be part of a mempool collision pair."""
        collision = _get_collision_data(features)
        if not collision:
            return False
        return collision.get("shared_inputs", 0) >= 1

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        collision = _get_collision_data(features)
        if not collision:
            return ScorerResult(score=0.0)

        # Sub-score 1: collision outcome (weight = 0.35)
        outcome = collision.get("outcome", "BOTH_PENDING")
        s_outcome = _OUTCOME_SCORES.get(outcome, 0.5)

        # Sub-score 2: mempool_delta_ms reciprocal (weight = 0.30)
        delta_ms = max(collision.get("delta_ms", 10000), 1.0)
        delta_inv = 1.0 / delta_ms
        s_delta = normalise(delta_inv, p50=_DELTA_INV_P50, p99=_DELTA_INV_P99)

        # Sub-score 3: attacker recurrence (weight = 0.25)
        win_count = collision.get("attacker_win_count", 0)
        p50_r, p99_r, bl1 = resolve_baseline(
            "collision_win_count", "per_cluster", "__global__",
        )
        if bl1 == "missing":
            p50_r, p99_r = 0.0, 5.0  # bootstrap
        s_recurrence = normalise(win_count, p50=p50_r, p99=p99_r)

        # Sub-score 4: structural similarity (weight = 0.10)
        fee = features.get("fee", 0)
        counterpart_fee = collision.get("counterpart_fee", 0)
        fee_sim = 1.0 - normalise(
            abs(fee - counterpart_fee), p50=_FEE_DELTA_P50, p99=_FEE_DELTA_P99,
        )

        ttl = features.get("raw_data", {}).get("timeToLive", 0) or 0
        counterpart_ttl = collision.get("counterpart_ttl", 0)
        ttl_sim = 1.0 - normalise(
            abs(ttl - counterpart_ttl), p50=_TTL_DELTA_P50, p99=_TTL_DELTA_P99,
        )

        change_link = 1.0 if collision.get("shares_change_address") else 0.0
        s_structure = (fee_sim + ttl_sim + change_link) / 3.0

        bl_source = bl1 if bl1 != "missing" else "bootstrap"

        raw = (
            0.35 * s_outcome
            + 0.30 * s_delta
            + 0.25 * s_recurrence
            + 0.10 * s_structure
        )
        final = round(max(0.0, min(1.0, raw)) * 100, 2)

        # Minimum recurrence gate (Polimi Section 4.5.4): cap below Critical
        # band when attacker has fewer than 3 collision wins in recent window
        _MIN_RECURRENCE_WINS = 3
        if win_count < _MIN_RECURRENCE_WINS and final >= 80:
            final = 79.0  # cap at top of High band

        reasons = []
        if s_outcome >= 0.8:
            reasons.append("confirmed_utxo_collision")
        if s_delta > 0.5:
            reasons.append("small_mempool_delta")
        if s_recurrence > 0.5:
            reasons.append("repeat_collision_winner")

        return ScorerResult(
            score=final,
            sub_scores={
                "collision_outcome": round(s_outcome, 4),
                "mempool_delta_inv": round(s_delta, 4),
                "attacker_recurrence": round(s_recurrence, 4),
                "structural_similarity": round(s_structure, 4),
                "delta_ms": round(delta_ms, 1),
                "outcome": outcome,
            },
            reasons=reasons,
            baseline_source=bl_source,
        )
