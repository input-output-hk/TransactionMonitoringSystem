"""Sandwich Attack scorer (Class 6).

Detects three-transaction exploits targeting DEX swap operations: tx_A (front)
buys ahead of the victim, the victim swaps at a worse rate, tx_B (back) sells
at the inflated price to capture profit.

This scorer operates on **transaction triples** identified by the DEX
interaction tracker.  When no sandwich candidate data is available, the gate
returns False.

Sub-scores (Polimi Section 4.6.3):
  attacker_link       (0.30): tx_A and tx_B share address cluster
  swap_rate_delta     (0.30): victim's rate deterioration, fixed anchors
  price_impact        (0.20): per-pool baseline
  profit              (0.10): per-pool baseline
  recurrence          (0.10): attacker cluster recurrence

Infrastructure dependency: DEX pool address registry and swap parsing must
be populated by the ingestion/analysis layer.  Until that infrastructure is
built, this scorer's gate will not pass.
"""

import logging
from typing import Any, Dict, Optional

from app.analysis.normalise import normalise
from app.analysis.scorer_config import (
    get as _get_cfg,
    anchor as _anchor,
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import BaseScorer, ScorerResult, finalise_score

logger = logging.getLogger(__name__)

_CFG = _get_cfg("sandwich")
_W = _CFG["weights"]
_FIXED = _CFG["fixed_anchors"]
_BOOT = _CFG["bootstrap_anchors"]
_LINK = _CFG["link_scores"]
_REASON_T = _CFG["reason_thresholds"]
W_SLOTS = int(_CFG["window_slots"])
_MIN_PROFIT_LOVELACE = int(_CFG["min_profit_lovelace"])

EPSILON = 1e-6


def _get_sandwich_data(features: Dict[str, Any]) -> Optional[Dict]:
    """Extract sandwich candidate data from features if available.

    The engine populates features["sandwich"] when a tx is identified as
    the victim in a candidate sandwich triple.  Structure:
      {
        "tx_a": str,
        "tx_b": str,
        "pool_id": str,
        "asset_pair": str,
        "attacker_linked": bool,
        "swap_rate_victim": float,
        "swap_rate_baseline": float,
        "price_impact_a": float,
        "profit_b": float,
        "attacker_sandwich_count": int,
        "slot_span": int,
      }
    """
    return features.get("sandwich")


class SandwichScorer(BaseScorer):
    name = "sandwich"

    def gate(self, features: Dict[str, Any]) -> bool:
        """Transaction must be identified as a sandwich victim."""
        sw = _get_sandwich_data(features)
        if not sw:
            return False
        # Gate: slot span within window
        return sw.get("slot_span", W_SLOTS + 1) <= W_SLOTS

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        sw = _get_sandwich_data(features)
        if not sw:
            return ScorerResult(score=0.0)

        pool_id = sw.get("pool_id", "")
        network = features.get("network", "")

        # Sub-score 1: attacker link
        s_link = float(_LINK["linked"]) if sw.get("attacker_linked") else float(_LINK["unlinked"])

        # Sub-score 2: swap_rate_delta
        rate_victim = sw.get("swap_rate_victim", 0.0)
        rate_baseline = sw.get("swap_rate_baseline", 0.0)
        if rate_baseline > 0:
            rate_delta = (rate_victim - rate_baseline) / (rate_baseline + EPSILON)
        else:
            rate_delta = 0.0
        # More negative delta = worse for victim = higher score
        p50_rd, p99_rd = _anchor(_FIXED, "rate_delta")
        s_rate = normalise(-rate_delta, p50=p50_rd, p99=p99_rd)

        # Sub-score 3: price_impact of tx_A
        impact = sw.get("price_impact_a", 0.0)
        p50_pi, p99_pi, bl1 = _resolve(
            "price_impact", "per_policy", pool_id, network,
            _BOOT, "price_impact",
        )
        s_impact = normalise(impact, p50=p50_pi, p99=p99_pi)

        # Sub-score 4: profit of tx_B
        profit = sw.get("profit_b", 0.0)
        p50_pr, p99_pr, _ = _resolve(
            "swap_profit", "per_policy", pool_id, network,
            _BOOT, "swap_profit",
        )
        s_profit = normalise(profit, p50=p50_pr, p99=p99_pr)

        # Sub-score 5: attacker recurrence
        sandwich_count = sw.get("attacker_sandwich_count", 0)
        p50_sc, p99_sc, _ = _resolve(
            "sandwich_count", "per_cluster", "__global__", network,
            _BOOT, "attacker_recurrence",
        )
        s_recurrence = normalise(sandwich_count, p50=p50_sc, p99=p99_sc)

        bl_source = bl1

        raw = (
            float(_W["link"]) * s_link
            + float(_W["rate"]) * s_rate
            + float(_W["impact"]) * s_impact
            + float(_W["profit"]) * s_profit
            + float(_W["recurrence"]) * s_recurrence
        )
        final = finalise_score(raw)

        # Economic suppression: a structural triple where the attacker
        # extracted no material ADA profit is not a sandwich. Suppress it
        # entirely (score -1, removed from max_class selection) rather than
        # band-capping, so it produces no alert at any band. sub_scores are
        # retained for observability, including the raw profit that drove the
        # decision. profit_b is computed in dex._attacker_net_ada; the
        # detector also excludes pool/batcher (script-address) clusters, so
        # the surviving candidates here are wallet attackers.
        if profit < _MIN_PROFIT_LOVELACE:
            return ScorerResult.no_finding(
                sub_scores={
                    "attacker_link": round(s_link, 4),
                    "swap_rate_delta": round(s_rate, 4),
                    "price_impact": round(s_impact, 4),
                    "profit": round(s_profit, 4),
                    "attacker_recurrence": round(s_recurrence, 4),
                    "pool_id": pool_id,
                    "attacker_profit_lovelace": int(profit),
                },
                baseline_source=bl_source,
            )

        reasons = []
        if s_link >= float(_REASON_T["link"]):
            reasons.append("attacker_txs_linked")
        if s_rate > float(_REASON_T["rate"]):
            reasons.append("victim_rate_deterioration")
        if s_impact > float(_REASON_T["impact"]):
            reasons.append("significant_price_impact")

        return ScorerResult(
            score=final,
            sub_scores={
                "attacker_link": round(s_link, 4),
                "swap_rate_delta": round(s_rate, 4),
                "price_impact": round(s_impact, 4),
                "profit": round(s_profit, 4),
                "attacker_recurrence": round(s_recurrence, 4),
                "pool_id": pool_id,
                "rate_delta_raw": round(rate_delta, 6),
            },
            reasons=reasons,
            baseline_source=bl_source,
            evidence={
                "pool_id": pool_id,
                "asset_pair": sw.get("asset_pair", ""),
                "tx_a_hash": sw.get("tx_a", ""),
                "tx_b_hash": sw.get("tx_b", ""),
                "slot_span": int(sw.get("slot_span", 0)),
                "swap_rate_victim": float(rate_victim),
                "swap_rate_baseline": float(rate_baseline),
                "rate_delta_pct": round(rate_delta * 100, 4),
                "price_impact_raw": float(impact),
                "attacker_profit_lovelace": int(profit),
                "attacker_linked": bool(sw.get("attacker_linked")),
                "attacker_sandwich_count": int(sandwich_count),
            },
        )
