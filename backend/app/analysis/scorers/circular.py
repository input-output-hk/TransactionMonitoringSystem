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
  cycle_recurrence     (0.30): per-cluster baseline; earlier High cycles of
                               the same ring (a shared intermediary)
  recipient_entropy    (0.20): inverted, fixed anchors; share of this cycle's
                               intermediaries the origin's earlier cycles reused,
                               for a cycle that passes its value along
                               (circular.recycling_min_amount_similarity)
  auxiliary            (0.10): round amounts + temporal concentration
  speed                (0.10): inter-hop slot delta reciprocal, fixed anchors

A cycle with no speed, no reused path and no recurrence whose hops are further
apart than an automated ring moves (circular.slow_one_off_min_hop_slots) is
capped at the top of Informational: its preserved amount and round amounts
alone are what a deposit to an exchange and the withdrawal back look like.

Infrastructure dependency: transfer graph construction and cycle detection
(bounded 6-hop BFS from tx sender) must be run by a background analysis
task.  Until that infrastructure is built, this scorer's gate will not pass.
"""

import logging
from typing import Any

from app.analysis.normalise import BAND_INFORMATIONAL_MAX, EPSILON, normalise
from app.analysis.scorer_config import (
    anchor as _anchor,
)
from app.analysis.scorer_config import (
    get as _get_cfg,
)
from app.analysis.scorer_config import (
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import BaseScorer, ScorerResult, finalise_score

logger = logging.getLogger(__name__)

_CFG = _get_cfg("circular")
_W = _CFG["weights"]
_FIXED = _CFG["fixed_anchors"]
_BOOT = _CFG["bootstrap_anchors"]
_CYCLE = _CFG["cycle"]
_REASON_T = float(_CFG["reason_threshold"])
# Band contract (cap must sit inside the Moderate band) is enforced at
# config load: scorer_config._BAND_INVARIANTS.
_MODERATE_CAP = float(_CFG["moderate_cap"])
_STRUCTURAL_CORROBORATION_FLOOR = float(_CFG["structural_corroboration_floor"])
# Mean slots between hops from which a cycle with no behavioural signal is a
# slow one-off round trip rather than an automated ring (the config says why).
_SLOW_ONE_OFF_MIN_HOP_SLOTS = int(_CFG["slow_one_off_min_hop_slots"])

FEE_TOLERANCE_MULTIPLIER = float(_CYCLE["fee_tolerance_multiplier"])
FEE_TOLERANCE_STRICT = float(_CYCLE["fee_tolerance_strict"])
_PER_HOP_FEE = float(_CYCLE["per_hop_fee_estimate"])
_MIN_LEN = int(_CYCLE["min_length"])
_MAX_LEN = int(_CYCLE["max_length"])
# Fallback inter-hop delta (slots) when a cycle record carries no measured
# cadence. Shared with the graph builder via config so the two cannot drift.
_DEFAULT_INTER_HOP_DELTA_SLOTS = int(_CYCLE["default_inter_hop_delta_slots"])


def _get_cycle_data(features: dict[str, Any]) -> dict | None:
    """Extract cycle detection data from features if available.

    The engine populates features["cycle"] when a tx is part of a detected
    transfer cycle.  Structure:
      {
        "cycle_length": int,
        "addresses": list[str],
        "amount_similarity": float,
        "net_loss_ratio": float,
        "recurrence_count": int,
        "prior_cycles_in_window": int,
        "recycled_share": float,
        "recipient_entropy": float,
        "round_amount_flag": bool,
        "temporal_concentration": float,
        "mean_inter_hop_delta_slots": float,
        "origin_cluster": str,
        "intermediaries": list[str],
        "origin_keys": list[str],
        "history_unavailable": bool,
      }
    """
    return features.get("cycle")


def _estimate_fee_ratio(cycle_length: int) -> float:
    """Estimate expected fee-only loss ratio for a cycle of k hops.

    Each hop costs roughly 0.17-0.30 ADA in fees.  For a cycle transferring
    ~10 ADA through k hops, the fee ratio is approximately k * 0.2 / 10.
    """
    return cycle_length * _PER_HOP_FEE


class CircularScorer(BaseScorer):
    name = "circular"

    def gate(self, features: dict[str, Any]) -> bool:
        """Transaction must be part of a detected transfer cycle."""
        cycle = _get_cycle_data(features)
        if not cycle:
            return False

        length = cycle.get("cycle_length", 0)
        if not (_MIN_LEN <= length <= _MAX_LEN):
            return False

        # Net loss must be consistent with fee-only loss
        net_loss = cycle.get("net_loss_ratio", 1.0)
        expected = _estimate_fee_ratio(length)
        if net_loss > expected * FEE_TOLERANCE_MULTIPLIER:
            return False

        return True

    def score(self, features: dict[str, Any]) -> ScorerResult:
        cycle = _get_cycle_data(features)
        if not cycle:
            return ScorerResult(score=0.0)

        origin = cycle.get("origin_cluster", "__unknown__")
        network = features.get("network", "")

        # Sub-score 1: amount_similarity
        amt_sim = cycle.get("amount_similarity", 0.0)
        p50_a, p99_a = _anchor(_FIXED, "amount_sim")
        s_amount = normalise(amt_sim, p50=p50_a, p99=p99_a)

        # Sub-score 2: cycle_recurrence
        recurrence = cycle.get("recurrence_count", 0)
        p50_r, p99_r, bl1 = _resolve(
            "cycle_recurrence",
            "per_cluster",
            origin,
            network,
            _BOOT,
            "attacker_recurrence",
        )
        s_recurrence = normalise(recurrence, p50=p50_r, p99=p99_r)

        # Sub-score 3: recipient_entropy inverted
        entropy = cycle.get("recipient_entropy", 1.0)
        # Fixed anchors are inverted (p50 > p99): high entropy = normal.
        # Normalise (1 - entropy) against (1 - p50_e, 1 - p99_e).
        p50_e, p99_e = _anchor(_FIXED, "entropy")
        s_entropy = normalise(1.0 - entropy, p50=1.0 - p50_e, p99=1.0 - p99_e)

        # Sub-score 4: auxiliary (round amounts + temporal concentration)
        s_round = 1.0 if cycle.get("round_amount_flag") else 0.0
        temporal = cycle.get("temporal_concentration", 0.0)
        p50_t, p99_t = _anchor(_FIXED, "temporal")
        s_timing = normalise(temporal, p50=p50_t, p99=p99_t)
        s_auxiliary = (s_round + s_timing) / 2.0

        # Sub-score 5: inter-hop speed
        hop_delta = max(
            cycle.get("mean_inter_hop_delta_slots", _DEFAULT_INTER_HOP_DELTA_SLOTS),
            EPSILON,
        )
        hop_inv = 1.0 / hop_delta
        p50_h, p99_h = _anchor(_FIXED, "hop_delta_inv")
        s_speed = normalise(hop_inv, p50=p50_h, p99=p99_h)

        bl_source = bl1

        hops = cycle.get("hops") or []
        # Stored whether or not the cycle is suppressed below: the origin's later
        # cycles read it back through the tx_class_scores circular_* columns
        # (clickhouse_schema._CIRCULAR_HISTORY_COLUMNS), so a structural-only
        # pass still counts as history for the next one.
        evidence = {
            "cycle_length": int(cycle.get("cycle_length", 0)),
            "net_loss_ratio": round(cycle.get("net_loss_ratio", 0), 4),
            "returned_lovelace": int(cycle.get("returned_lovelace", 0)),
            "amount_similarity_raw": round(amt_sim, 4),
            "hops": hops,
            "mean_inter_hop_slots": float(cycle.get("mean_inter_hop_delta_slots", 0)),
            "origin_cluster": cycle.get("origin_cluster", ""),
            "first_slot": int(hops[0].get("slot", 0)) if hops else 0,
            "round_amount_flag": bool(cycle.get("round_amount_flag")),
            "recipient_entropy": round(float(entropy), 4),
            "recycled_share": round(float(cycle.get("recycled_share", 0.0)), 4),
            "prior_cycles_in_window": int(cycle.get("prior_cycles_in_window", 0)),
            "intermediaries": list(cycle.get("intermediaries") or []),
            "origin_keys": list(cycle.get("origin_keys") or []),
        }

        raw = (
            float(_W["amount"]) * s_amount
            + float(_W["recurrence"]) * s_recurrence
            + float(_W["entropy"]) * s_entropy
            + float(_W["auxiliary"]) * s_auxiliary
            + float(_W["speed"]) * s_speed
        )
        final = finalise_score(raw)

        # Fee-ratio cap: if net loss exceeds the strict fee-only threshold,
        # the cycle may be incidental transfers rather than deliberate layering.
        net_loss = cycle.get("net_loss_ratio", 0.0)
        expected_fee = _estimate_fee_ratio(cycle.get("cycle_length", 2))

        # Structural-only suppression: amount_similarity + cycle_recurrence
        # alone sum to 0.60 in weights, which tips a plain A->script->A Plutus
        # interaction (pool swap, state machine) into an alerting band. When the
        # corroborating axes (entropy / auxiliary / speed) all fall below the
        # configured floor, the cycle is structurally indistinguishable from
        # benign DeFi composition and carries no evidence of deliberate
        # layering, so it is suppressed entirely (score -1, no finding) rather
        # than surfaced at a capped Moderate. A real wash/layering cycle has
        # low recipient entropy and/or tight temporal concentration and is not
        # structural-only, so it is unaffected. Floor tunable via
        # circular.structural_corroboration_floor.
        structural_only = (s_entropy + s_auxiliary + s_speed) < _STRUCTURAL_CORROBORATION_FLOOR
        if structural_only:
            # Recall-first escape: recipient_entropy is INVERTED (low == recycled
            # nodes), so AML layering that launders through many FRESH
            # intermediary addresses scores high entropy -> s_entropy ~ 0 and
            # looks structural-only. But a cycle that closes to origin with
            # strongly preserved amounts AND repeated recurrence is deliberate
            # layering regardless of address freshness. Suppressing it to
            # no-finding would miss exactly that attack, so when both the amount
            # and recurrence axes are high enough to earn their reason flags,
            # surface the cycle at a capped Moderate instead of dropping it. A
            # genuinely benign structural cycle (e.g. a plain pool swap) does not
            # clear both bars and is still suppressed.
            # Only genuine multi-hop rings (length >= _MIN_LEN, which the gate
            # already enforces in production) qualify: a 2-hop A->script->A
            # round-trip repeated many times is a bot/DeFi interaction, not
            # layering, and stays suppressed.
            recurring_layering = (
                cycle.get("cycle_length", 0) >= _MIN_LEN
                and s_amount > _REASON_T
                and s_recurrence > _REASON_T
            )
            if not recurring_layering:
                return ScorerResult.no_finding(
                    sub_scores={
                        "amount_similarity": round(s_amount, 4),
                        "cycle_recurrence": round(s_recurrence, 4),
                        "recipient_entropy_inv": round(s_entropy, 4),
                        "auxiliary": round(s_auxiliary, 4),
                        "speed": round(s_speed, 4),
                        "cycle_length": cycle.get("cycle_length", 0),
                    },
                    baseline_source=bl_source,
                    evidence=evidence,
                )
            final = min(final, _MODERATE_CAP)

        # Fee-ratio cap: a corroborated cycle that still loses more than the
        # strict fee-only threshold may be incidental layering rather than a
        # tight wash, so cap it at Moderate instead of letting it reach High.
        if final > _MODERATE_CAP and net_loss > expected_fee * FEE_TOLERANCE_STRICT:
            final = _MODERATE_CAP

        # One-off, slow and never recycled: with no speed, no reused path and no
        # earlier High of the ring, a cycle carries only its preserved amount and
        # the auxiliary axis (round amounts, timing). When its hops are also
        # further apart than an automated ring moves, that is what a deposit to
        # an exchange and the withdrawal back look like, as five of the six such
        # Moderates in the stored mainnet history were. It stays visible, at the
        # top of Informational. A ring at block pace keeps its Moderate: the
        # speed axis reads zero from one hop per block, the pace the modeled
        # attack moves at. Judged on the structural floor without the auxiliary
        # axis, so any behavioural signal lifts the cap.
        if (
            hop_delta >= _SLOW_ONE_OFF_MIN_HOP_SLOTS
            and (s_entropy + s_speed + s_recurrence) < _STRUCTURAL_CORROBORATION_FLOOR
        ):
            final = min(final, BAND_INFORMATIONAL_MAX)

        reasons = []
        if s_amount > _REASON_T:
            reasons.append("high_amount_preservation")
        if s_recurrence > _REASON_T:
            reasons.append("repeated_cycle_pattern")
        if s_entropy > _REASON_T:
            reasons.append("low_recipient_diversity")
        if s_speed > _REASON_T:
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
            evidence=evidence,
        )
