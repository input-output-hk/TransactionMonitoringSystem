"""Token Dust attack scorer (Class 1).

Detects UTxOs deposited at script addresses with abnormally large numbers of
distinct native assets, bloating the Value field CBOR encoding.  The attack
vector targets validators that iterate over the token bundle, causing
unbounded computation or exceeding protocol-level UTxO size limits.

Scoring is per-UTxO; the transaction score is the max across all outputs.

Sub-scores (Polimi Section 4.1.3):
  value_cbor_bytes      (0.35): CBOR byte footprint of the Value field
  unique_assetclass_count (0.35): distinct asset classes across policies
  lovelace_amount       (0.15): inverted; low ADA relative to asset count
  sender_recurrence     (0.15): repeated deposits from same cluster
"""

import logging
from typing import Any, Dict

from app.analysis.normalise import normalise, normalise_inverted, resolve_baseline
from app.analysis.scorers.base import BaseScorer, ScorerResult
from app.analysis import features as feat_mod

logger = logging.getLogger(__name__)


class TokenDustScorer(BaseScorer):
    name = "token_dust"

    def gate(self, features: Dict[str, Any]) -> bool:
        """At least one output must be a script address with native assets."""
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
            # Has native assets beyond lovelace
            if any(k not in ("lovelace", "ada") for k in value):
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
            if not any(k not in ("lovelace", "ada") for k in value):
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
        policy_count, token_count = feat_mod._count_assets(value)

        # Resolve baselines (per-script -> global -> bootstrap)
        p50_cb, p99_cb, bl1 = resolve_baseline(
            "value_cbor_bytes", "per_script", address,
        )
        if bl1 == "missing":
            p50_cb, p99_cb = 50.0, 500.0  # bootstrap

        p50_ac, p99_ac, bl2 = resolve_baseline(
            "unique_token_count", "per_script", address,
        )
        if bl2 == "missing":
            p50_ac, p99_ac = 1.0, 20.0  # bootstrap

        p50_ada, p99_ada, bl3 = resolve_baseline(
            "ada_amount", "per_script", address,
        )
        if bl3 == "missing":
            p50_ada, p99_ada = 2_000_000.0, 50_000_000.0  # bootstrap

        bl_source = bl1 if bl1 != "missing" else "bootstrap"

        # Sub-scores
        s_bytes = normalise(value_cbor, p50=p50_cb, p99=p99_cb)
        s_assets = normalise(token_count, p50=p50_ac, p99=p99_ac)
        s_ada = normalise_inverted(ada_amount, p50=p50_ada, p99=p99_ada)
        # Sender recurrence: requires entity clustering (deferred to mainnet)
        s_recurrence = 0.0

        raw = (
            0.35 * s_bytes
            + 0.35 * s_assets
            + 0.15 * s_ada
            + 0.15 * s_recurrence
        )
        final = round(max(0.0, min(1.0, raw)) * 100, 2)

        reasons = []
        if s_bytes > 0.5:
            reasons.append("high_value_cbor_bytes")
        if s_assets > 0.5:
            reasons.append("many_distinct_assets")
        if s_ada > 0.5:
            reasons.append("low_lovelace_amount")

        return ScorerResult(
            score=final,
            sub_scores={
                "value_cbor_bytes": round(s_bytes, 4),
                "unique_assetclass_count": round(s_assets, 4),
                "lovelace_inverted": round(s_ada, 4),
                "sender_recurrence": round(s_recurrence, 4),
            },
            reasons=reasons,
            baseline_source=bl_source,
        )
