"""Large Datum attack scorer (Class 3).

Detects UTxOs at script addresses with abnormally large inline datums (or
resolvable datum hashes).  The bloat originates from the datum component
exclusively: the Value field remains normal (only ADA or small standard assets).

The key structural separator from Classes 1-2 is datum_ratio: the fraction
of total UTxO bytes occupied by the datum.  Values above 0.60 are strong
indicators of datum-bloat rather than general value-field bloat.

Scoring is per-UTxO; the transaction score is the max across all outputs.

Sub-scores (Polimi Section 4.3.3):
  datum_bytes          (0.40): absolute byte size, per-script baseline
  datum_ratio          (0.35): datum_bytes / utxo_total_bytes, fixed anchors
  value_cbor_bytes_inv (0.15): inverted; lean Value field = datum-bloat signature
  sender_recurrence    (0.10): repeated bloated-datum deposits
"""

import logging
from typing import Any, Dict

from app.analysis.normalise import normalise, normalise_inverted, resolve_baseline
from app.analysis.scorers.base import BaseScorer, ScorerResult
from app.analysis import features as feat_mod

logger = logging.getLogger(__name__)

# Fixed anchors for datum_ratio (Polimi Section 5.4)
_DATUM_RATIO_P50 = 0.20
_DATUM_RATIO_P99 = 0.60


class LargeDatumScorer(BaseScorer):
    name = "large_datum"

    def gate(self, features: Dict[str, Any]) -> bool:
        """Script address with datum present (inline or hash)."""
        raw_data = features.get("raw_data")
        if not raw_data or not isinstance(raw_data, dict):
            return False
        outputs = raw_data.get("outputs", [])
        for out in outputs:
            addr = out.get("address", "")
            if not feat_mod.is_script_address(addr):
                continue
            datum_flag, datum_bytes = feat_mod._extract_datum_info(out)
            if datum_flag > 0 and datum_bytes > 0:
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
            datum_flag, datum_bytes = feat_mod._extract_datum_info(out)
            if datum_flag == 0 or datum_bytes == 0:
                continue

            result = self._score_utxo(out, addr, datum_bytes, network)
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
        self, output: Dict, address: str, datum_bytes: int, network: str,
    ) -> ScorerResult:
        import json

        value = output.get("value", {})
        if not isinstance(value, dict):
            value = {"lovelace": 0}

        value_cbor = feat_mod._estimate_value_cbor_bytes(value)

        # Estimate total UTxO bytes
        addr_bytes = len(address.encode()) if address else 0
        script_ref = output.get("script")
        script_bytes = len(json.dumps(script_ref).encode()) if script_ref else 0
        utxo_total = addr_bytes + value_cbor + datum_bytes + script_bytes

        datum_ratio = datum_bytes / (utxo_total + 1e-6) if utxo_total > 0 else 0.0

        # datum_bytes: per-script baseline
        p50_db, p99_db, bl1 = resolve_baseline(
            "datum_bytes", "per_script", address,
        )
        if bl1 == "missing":
            p50_db, p99_db = 50.0, 2000.0  # bootstrap

        # value_cbor_bytes: per-script baseline (for inversion)
        p50_cb, p99_cb, bl2 = resolve_baseline(
            "value_cbor_bytes", "per_script", address,
        )
        if bl2 == "missing":
            p50_cb, p99_cb = 50.0, 500.0  # bootstrap

        bl_source = bl1 if bl1 != "missing" else "bootstrap"

        # Sub-scores
        s_datum = normalise(datum_bytes, p50=p50_db, p99=p99_db)
        s_ratio = normalise(datum_ratio, p50=_DATUM_RATIO_P50, p99=_DATUM_RATIO_P99)
        s_value_inv = normalise_inverted(value_cbor, p50=p50_cb, p99=p99_cb)
        s_recurrence = 0.0  # requires entity clustering (deferred to mainnet)

        raw = (
            0.40 * s_datum
            + 0.35 * s_ratio
            + 0.15 * s_value_inv
            + 0.10 * s_recurrence
        )
        final = round(max(0.0, min(1.0, raw)) * 100, 2)

        reasons = []
        if s_datum > 0.5:
            reasons.append("large_datum_bytes")
        if s_ratio > 0.5:
            reasons.append("high_datum_ratio")
        if s_value_inv > 0.5:
            reasons.append("lean_value_field")

        return ScorerResult(
            score=final,
            sub_scores={
                "datum_bytes": round(s_datum, 4),
                "datum_ratio": round(s_ratio, 4),
                "value_cbor_bytes_inverted": round(s_value_inv, 4),
                "sender_recurrence": round(s_recurrence, 4),
            },
            reasons=reasons,
            baseline_source=bl_source,
        )
