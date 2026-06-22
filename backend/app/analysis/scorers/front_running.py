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

from app.analysis.features import extract_ttl
from app.analysis.normalise import BAND_CRITICAL_THRESHOLD, normalise
from app.analysis.scorer_config import (
    get as _get_cfg,
    anchor as _anchor,
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import BaseScorer, ScorerResult, finalise_score

logger = logging.getLogger(__name__)

_CFG = _get_cfg("front_running")
_W = _CFG["weights"]
_FIXED = _CFG["fixed_anchors"]
_BOOT = _CFG["bootstrap_anchors"]
_OUTCOME_SCORES: Dict[str, float] = {
    k: float(v) for k, v in _CFG["outcome_scores"].items()
}
_REASON_T = _CFG["reason_thresholds"]
_MIN_RECURRENCE_WINS = int(_CFG["min_recurrence_wins"])
_HIGH_BAND_CAP = float(_CFG["high_band_cap"])
_DELTA_MS_DEFAULT = float(_CFG["delta_ms_default"])


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

        network = features.get("network", "")

        # Sub-score 1: collision outcome (weight = 0.35)
        outcome = collision.get("outcome", "BOTH_PENDING")
        s_outcome = _OUTCOME_SCORES.get(outcome, 0.5)

        # Sub-score 2: mempool_delta_ms reciprocal
        delta_ms = max(collision.get("delta_ms", _DELTA_MS_DEFAULT), 1.0)
        delta_inv = 1.0 / delta_ms
        p50_d, p99_d = _anchor(_FIXED, "mempool_delta_inv")
        s_delta = normalise(delta_inv, p50=p50_d, p99=p99_d)

        # Sub-score 3: attacker recurrence
        win_count = collision.get("attacker_win_count", 0)
        p50_r, p99_r, bl1 = _resolve(
            "collision_win_count", "per_cluster", "__global__", network,
            _BOOT, "attacker_recurrence",
        )
        s_recurrence = normalise(win_count, p50=p50_r, p99=p99_r)

        # Sub-score 4: structural similarity
        fee = features.get("fee", 0)
        counterpart_fee = collision.get("counterpart_fee", 0)
        p50_f, p99_f = _anchor(_FIXED, "fee_delta")
        fee_sim = 1.0 - normalise(
            abs(fee - counterpart_fee), p50=p50_f, p99=p99_f,
        )

        ttl = extract_ttl(features.get("raw_data", {}))
        counterpart_ttl = collision.get("counterpart_ttl", 0)
        p50_t, p99_t = _anchor(_FIXED, "ttl_delta")
        ttl_sim = 1.0 - normalise(
            abs(ttl - counterpart_ttl), p50=p50_t, p99=p99_t,
        )

        change_link = 1.0 if collision.get("shares_change_address") else 0.0
        s_structure = (fee_sim + ttl_sim + change_link) / 3.0

        bl_source = bl1

        raw = (
            float(_W["outcome"]) * s_outcome
            + float(_W["delta"]) * s_delta
            + float(_W["recurrence"]) * s_recurrence
            + float(_W["structure"]) * s_structure
        )
        final = finalise_score(raw)

        # Minimum recurrence gate: cap below Critical band when attacker has
        # fewer than _MIN_RECURRENCE_WINS collision wins in recent window.
        # Cap and recurrence floor tunable via front_running.high_band_cap /
        # min_recurrence_wins; the Critical threshold itself is the canonical
        # band boundary in normalise.BAND_CRITICAL_THRESHOLD.
        if win_count < _MIN_RECURRENCE_WINS and final >= BAND_CRITICAL_THRESHOLD:
            final = _HIGH_BAND_CAP

        reasons = []
        if s_outcome >= float(_REASON_T["outcome"]):
            reasons.append("confirmed_utxo_collision")
        if s_delta > float(_REASON_T["delta"]):
            reasons.append("small_mempool_delta")
        if s_recurrence > float(_REASON_T["recurrence"]):
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
            evidence={
                "delta_ms": round(delta_ms, 1),
                "outcome": outcome,
                "tx_role": collision.get("tx_role", ""),
                "counterpart_tx_hash": collision.get("counterpart_tx", ""),
                "shared_input_count": int(collision.get("shared_inputs", 0)),
                "tx_fee": int(fee or 0),
                "counterpart_fee": int(counterpart_fee or 0),
                "ttl": int(ttl or 0),
                "counterpart_ttl": int(counterpart_ttl or 0),
                "shares_change_address": bool(collision.get("shares_change_address")),
                "attacker_win_count": int(win_count),
                "attacker_win_count_24h": int(collision.get("attacker_win_count_24h", 0)),
            },
        )
