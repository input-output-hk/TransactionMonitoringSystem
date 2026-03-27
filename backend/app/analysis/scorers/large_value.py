"""Large Value attack scorer (Class 2).

Detects UTxOs at script addresses carrying a single asset (or at most 2) with
an astronomically large quantity.  Bloat is achieved through quantity magnitude
rather than variety: variable-length CBOR integers grow with magnitude, so
10^35 requires more bytes than 100.

Scoring is per-UTxO; the transaction score is the max across all outputs.

Sub-scores (Polimi Section 4.2.3):
  quantity_digits     (0.40): decimal digit count, per-policy baseline
  value_cbor_bytes    (0.35): high despite low asset count
  lovelace_amount     (0.10): inverted; typically minimal
  sender_recurrence   (0.15): repeated deposits suggest probing
"""

import logging
from typing import Any, Dict

from app.analysis.normalise import normalise, normalise_inverted, resolve_baseline
from app.analysis.scorers.base import BaseScorer, ScorerResult
from app.analysis import features as feat_mod

logger = logging.getLogger(__name__)


def _max_quantity_digits(value: Dict[str, Any]) -> int:
    """Return the number of decimal digits in the largest asset quantity."""
    max_digits = 0
    for key, val in value.items():
        if key in ("lovelace", "ada"):
            continue
        if isinstance(val, dict):
            for qty in val.values():
                d = len(str(abs(int(qty)))) if qty else 0
                max_digits = max(max_digits, d)
        else:
            d = len(str(abs(int(val)))) if val else 0
            max_digits = max(max_digits, d)
    return max_digits


def _primary_policy_id(value: Dict[str, Any]) -> str:
    """Return the policy ID of the asset with the largest quantity."""
    best_policy = ""
    best_qty = 0
    for key, val in value.items():
        if key in ("lovelace", "ada"):
            continue
        if isinstance(val, dict):
            for qty in val.values():
                q = abs(int(qty)) if qty else 0
                if q > best_qty:
                    best_qty = q
                    best_policy = key
        else:
            q = abs(int(val)) if val else 0
            if q > best_qty:
                best_qty = q
                best_policy = key
    return best_policy


class LargeValueScorer(BaseScorer):
    name = "large_value"

    def gate(self, features: Dict[str, Any]) -> bool:
        """Script address with at most 2 unique asset classes."""
        raw_data = features.get("raw_data")
        if not raw_data or not isinstance(raw_data, dict):
            return False
        outputs = raw_data.get("outputs", [])
        for out in outputs:
            addr = out.get("address", "")
            if not feat_mod.is_script_address(addr):
                continue
            value = out.get("value", {})
            if not isinstance(value, dict):
                continue
            _, token_count = feat_mod._count_assets(value)
            if 0 < token_count <= 2:
                return True
        return False

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        raw_data = features.get("raw_data", {})
        network = features.get("network", "")
        outputs = raw_data.get("outputs", [])

        best_score = 0.0
        best_sub = {}
        best_reasons = []
        best_bl_source = "missing"

        for out in outputs:
            addr = out.get("address", "")
            if not feat_mod.is_script_address(addr):
                continue
            value = out.get("value", {})
            if not isinstance(value, dict):
                continue
            _, token_count = feat_mod._count_assets(value)
            if token_count == 0 or token_count > 2:
                continue

            result = self._score_utxo(out, addr, network)
            if result.score > best_score:
                best_score = result.score
                best_sub = result.sub_scores
                best_reasons = result.reasons
                best_bl_source = result.baseline_source

        return ScorerResult(
            score=best_score,
            sub_scores=best_sub,
            reasons=best_reasons,
            baseline_source=best_bl_source,
        )

    def _score_utxo(
        self, output: Dict, address: str, network: str,
    ) -> ScorerResult:
        value = output.get("value", {})
        if not isinstance(value, dict):
            value = {"lovelace": 0}

        ada_obj = value.get("ada")
        if isinstance(ada_obj, dict):
            ada_amount = int(ada_obj.get("lovelace", 0))
        else:
            ada_amount = int(value.get("lovelace", 0))
        value_cbor = feat_mod._estimate_value_cbor_bytes(value)
        qty_digits = _max_quantity_digits(value)
        policy_id = _primary_policy_id(value)

        # quantity_digits: per-policy baseline (Polimi Section 4.2.3)
        p50_qd, p99_qd, bl1 = resolve_baseline(
            "quantity_digits", "per_policy", policy_id,
        ) if policy_id else (0.0, 1.0, "missing")
        if bl1 == "missing":
            p50_qd, p99_qd = 3.0, 18.0  # bootstrap

        # value_cbor_bytes: per-script baseline
        p50_cb, p99_cb, bl2 = resolve_baseline(
            "value_cbor_bytes", "per_script", address,
        )
        if bl2 == "missing":
            p50_cb, p99_cb = 50.0, 500.0  # bootstrap

        # lovelace_amount: per-script baseline
        p50_ada, p99_ada, bl3 = resolve_baseline(
            "ada_amount", "per_script", address,
        )
        if bl3 == "missing":
            p50_ada, p99_ada = 2_000_000.0, 50_000_000.0  # bootstrap

        bl_source = bl1 if bl1 != "missing" else "bootstrap"

        # Sub-scores
        s_digits = normalise(qty_digits, p50=p50_qd, p99=p99_qd)
        s_bytes = normalise(value_cbor, p50=p50_cb, p99=p99_cb)
        s_ada = normalise_inverted(ada_amount, p50=p50_ada, p99=p99_ada)
        s_recurrence = 0.0  # requires entity clustering (deferred to mainnet)

        raw = (
            0.40 * s_digits
            + 0.35 * s_bytes
            + 0.10 * s_ada
            + 0.15 * s_recurrence
        )
        final = round(max(0.0, min(1.0, raw)) * 100, 2)

        reasons = []
        if s_digits > 0.5:
            reasons.append("extreme_quantity_digits")
        if s_bytes > 0.5:
            reasons.append("high_value_cbor_for_few_assets")
        if s_ada > 0.5:
            reasons.append("low_lovelace_amount")

        return ScorerResult(
            score=final,
            sub_scores={
                "quantity_digits": round(s_digits, 4),
                "value_cbor_bytes": round(s_bytes, 4),
                "lovelace_inverted": round(s_ada, 4),
                "sender_recurrence": round(s_recurrence, 4),
            },
            reasons=reasons,
            baseline_source=bl_source,
        )
