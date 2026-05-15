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

from app.analysis.normalise import normalise, normalise_inverted
from app.analysis.scorer_config import (
    get as _get_cfg,
    anchor as _anchor,
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import BaseScorer, ScorerResult, finalise_score
from app.analysis.scorers.multiple_sat import _payment_credential
from app.analysis import features as feat_mod

logger = logging.getLogger(__name__)

_CFG = _get_cfg("large_datum")
_W = _CFG["weights"]
_FIXED = _CFG["fixed_anchors"]
_BOOT = _CFG["bootstrap_anchors"]
_REASON_T = float(_CFG["reason_threshold"])
_MIN_DATUM_BYTES = int(_CFG["gate"]["min_datum_bytes"])
_AGGREGATE_ENGAGEMENT_MIN = int(_CFG["aggregate_engagement_min"])


def _per_script_datum_bytes(outputs):
    """Return ``{payment_credential: total_datum_bytes}`` across script
    outputs.

    Aggregation is keyed by payment credential so the multi-output
    bloat shape ("N inflated outputs at the SAME contract") aggregates
    correctly across stake-credential variants of the same script, and
    does NOT aggregate across distinct contracts (where the carry-
    forward DoS mechanism does not apply). Falls back gracefully when
    ``_payment_credential`` cannot bech32-decode the address: the raw
    address is used as the key, so two outputs at the same raw address
    still group together.
    """
    by_script: Dict[str, int] = {}
    for out in outputs:
        addr = out.get("address", "")
        if not feat_mod.is_script_address(addr):
            continue
        datum_flag, datum_bytes = feat_mod._extract_datum_info(out)
        if datum_flag == 0:
            continue
        key = _payment_credential(addr)
        by_script[key] = by_script.get(key, 0) + datum_bytes
    return by_script


class LargeDatumScorer(BaseScorer):
    name = "large_datum"

    def gate(self, features: Dict[str, Any]) -> bool:
        """Engage scoring when either a single script datum exceeds the
        per-output floor (canonical DoS shape) OR the sum of datum bytes
        AT THE SAME SCRIPT crosses ``aggregate_engagement_min``
        (observability path for multi-output bloat).

        The per-output predicate is what produces an alert: only when
        one UTxO's datum saturates do downstream users have to copy
        bloated state. The aggregate predicate engages the scorer but
        does NOT contribute to ``max_score`` or band; it exists so the
        ``max_script_datum_bytes`` sub-score reaches storage when an
        attacker splits a bloat payload across N outputs of the same
        contract, each of size ``< min_datum_bytes``. Per-script
        aggregation prevents benign cross-contract DeFi composition
        (e.g. DEX-A 3.5KB state + DEX-B 3.5KB state) from engaging.
        """
        raw_data = features.get("raw_data")
        if not raw_data or not isinstance(raw_data, dict):
            return False
        outputs = raw_data.get("outputs", [])
        for out in outputs:
            addr = out.get("address", "")
            if not feat_mod.is_script_address(addr):
                continue
            _, datum_bytes = feat_mod._extract_datum_info(out)
            if datum_bytes >= _MIN_DATUM_BYTES:
                return True
        per_script = _per_script_datum_bytes(outputs)
        return any(v >= _AGGREGATE_ENGAGEMENT_MIN for v in per_script.values())

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        raw_data = features.get("raw_data", {})
        network = features.get("network", "")
        outputs = raw_data.get("outputs", [])

        # Per-script aggregate datum bytes. The largest same-script
        # aggregate is the observability metric: it identifies a single
        # contract under bloat pressure, not a tx-wide sum across
        # unrelated scripts. The per-output predicate below still drives
        # scoring; this is for analyst queries only.
        per_script = _per_script_datum_bytes(outputs)
        max_script_datum_bytes = max(per_script.values(), default=0)

        best_score = 0.0
        best_sub = {}
        best_reasons = []
        best_bl_source = "missing"

        for out in outputs:
            addr = out.get("address", "")
            if not feat_mod.is_script_address(addr):
                continue
            datum_flag, datum_bytes = feat_mod._extract_datum_info(out)
            if datum_flag == 0 or datum_bytes < _MIN_DATUM_BYTES:
                continue

            result = self._score_utxo(
                out, addr, datum_bytes, network, max_script_datum_bytes,
            )
            if result.score > best_score:
                best_score = result.score
                best_sub = result.sub_scores
                best_reasons = result.reasons
                best_bl_source = result.baseline_source

        if best_sub:
            return ScorerResult(
                score=best_score,
                sub_scores=best_sub,
                reasons=best_reasons,
                baseline_source=best_bl_source,
            )

        # Aggregate-only engagement path: gate fired because the
        # per-script aggregate crossed `aggregate_engagement_min`, but no
        # single output passed the per-output threshold. Surface the
        # observability metric while returning score=-1 so the engine
        # does NOT select `large_datum` as `max_class` (-1 is filtered
        # out by `applicable = {k: v ... if v >= 0}`); writing -1 to the
        # column matches the existing "scorer didn't produce a finding"
        # convention.
        return ScorerResult(
            score=-1.0,
            sub_scores={"max_script_datum_bytes": float(max_script_datum_bytes)},
            reasons=[],
            baseline_source="missing",
        )

    def _score_utxo(
        self, output: Dict, address: str, datum_bytes: int, network: str,
        max_script_datum_bytes: int,
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
        p50_db, p99_db, bl1 = _resolve(
            "datum_bytes", "per_script", address, network,
            _BOOT, "datum_bytes",
        )
        # value_cbor_bytes: per-script baseline (for inversion)
        p50_cb, p99_cb, _ = _resolve(
            "value_cbor_bytes", "per_script", address, network,
            _BOOT, "value_cbor_bytes",
        )
        p50_r, p99_r = _anchor(_FIXED, "datum_ratio")
        bl_source = bl1

        # Sub-scores
        s_datum = normalise(datum_bytes, p50=p50_db, p99=p99_db)
        s_ratio = normalise(datum_ratio, p50=p50_r, p99=p99_r)
        s_value_inv = normalise_inverted(value_cbor, p50=p50_cb, p99=p99_cb)
        s_recurrence = 0.0  # requires entity clustering (deferred to mainnet)

        raw = (
            float(_W["datum_bytes"]) * s_datum
            + float(_W["datum_ratio"]) * s_ratio
            + float(_W["value_cbor_inv"]) * s_value_inv
            + float(_W["recurrence"]) * s_recurrence
        )
        final = finalise_score(raw)

        reasons = []
        if s_datum > _REASON_T:
            reasons.append("large_datum_bytes")
        if s_ratio > _REASON_T:
            reasons.append("high_datum_ratio")
        if s_value_inv > _REASON_T:
            reasons.append("lean_value_field")

        return ScorerResult(
            score=final,
            sub_scores={
                "datum_bytes": round(s_datum, 4),
                "datum_ratio": round(s_ratio, 4),
                "value_cbor_bytes_inverted": round(s_value_inv, 4),
                "sender_recurrence": round(s_recurrence, 4),
                "max_script_datum_bytes": float(max_script_datum_bytes),
            },
            reasons=reasons,
            baseline_source=bl_source,
        )
