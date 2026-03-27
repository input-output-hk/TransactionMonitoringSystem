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

from app.analysis.normalise import normalise, resolve_baseline
from app.analysis.scorers.base import BaseScorer, ScorerResult

logger = logging.getLogger(__name__)

# Fixed anchors (Polimi Section 5.4)
_RATE_DELTA_P50 = 0.0
_RATE_DELTA_P99 = 0.15   # 15% deterioration

# Default slot window for triple detection
W_SLOTS = 5

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

        # Sub-score 1: attacker link (weight = 0.30)
        s_link = 1.0 if sw.get("attacker_linked") else 0.2

        # Sub-score 2: swap_rate_delta (weight = 0.30)
        rate_victim = sw.get("swap_rate_victim", 0.0)
        rate_baseline = sw.get("swap_rate_baseline", 0.0)
        if rate_baseline > 0:
            rate_delta = (rate_victim - rate_baseline) / (rate_baseline + EPSILON)
        else:
            rate_delta = 0.0
        # More negative delta = worse for victim = higher score
        s_rate = normalise(-rate_delta, p50=_RATE_DELTA_P50, p99=_RATE_DELTA_P99)

        # Sub-score 3: price_impact of tx_A (weight = 0.20)
        impact = sw.get("price_impact_a", 0.0)
        p50_pi, p99_pi, bl1 = resolve_baseline(
            "price_impact", "per_policy", pool_id,
        )
        if bl1 == "missing":
            p50_pi, p99_pi = 0.0, 0.05  # bootstrap
        s_impact = normalise(impact, p50=p50_pi, p99=p99_pi)

        # Sub-score 4: profit of tx_B (weight = 0.10)
        profit = sw.get("profit_b", 0.0)
        p50_pr, p99_pr, bl2 = resolve_baseline(
            "swap_profit", "per_policy", pool_id,
        )
        if bl2 == "missing":
            p50_pr, p99_pr = 0.0, 5_000_000.0  # bootstrap (lovelace)
        s_profit = normalise(profit, p50=p50_pr, p99=p99_pr)

        # Sub-score 5: attacker recurrence (weight = 0.10)
        sandwich_count = sw.get("attacker_sandwich_count", 0)
        p50_sc, p99_sc, bl3 = resolve_baseline(
            "sandwich_count", "per_cluster", "__global__",
        )
        if bl3 == "missing":
            p50_sc, p99_sc = 0.0, 5.0  # bootstrap
        s_recurrence = normalise(sandwich_count, p50=p50_sc, p99=p99_sc)

        bl_source = bl1 if bl1 != "missing" else "bootstrap"

        raw = (
            0.30 * s_link
            + 0.30 * s_rate
            + 0.20 * s_impact
            + 0.10 * s_profit
            + 0.10 * s_recurrence
        )
        final = round(max(0.0, min(1.0, raw)) * 100, 2)

        # Minimum profit gate (Polimi Section 4.6.4): if profit is below
        # a minimum economically meaningful threshold (~median tx fee),
        # cap below Critical band as likely coincidental
        _MIN_PROFIT_LOVELACE = 200_000  # ~0.2 ADA, approximate median fee
        if profit < _MIN_PROFIT_LOVELACE and final >= 80:
            final = 79.0  # cap at top of High band

        reasons = []
        if s_link >= 0.8:
            reasons.append("attacker_txs_linked")
        if s_rate > 0.5:
            reasons.append("victim_rate_deterioration")
        if s_impact > 0.5:
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
        )
