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
from typing import Any, Dict, Tuple

from app.analysis.normalise import (
    BAND_LOW_MAX,
    normalise,
    normalise_inverted,
)
from app.analysis.scorer_config import (
    get as _get_cfg,
    anchor as _anchor,
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import BaseScorer, ScorerResult, finalise_score
from app.analysis import features as feat_mod

logger = logging.getLogger(__name__)

_CFG = _get_cfg("large_value")
_W = _CFG["weights"]
_BOOT = _CFG["bootstrap_anchors"]
_REASON_T = float(_CFG["reason_threshold"])
_MIN_DIGITS_SUBSCORE = float(_CFG["min_digits_subscore"])


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


def _primary_asset(value: Dict[str, Any]) -> Tuple[str, str, int]:
    """Return ``(policy_id, asset_name_hex, quantity)`` for the largest asset.

    Returns empty strings and 0 when the value has no native assets.
    """
    best = ("", "", 0)
    for key, val in value.items():
        if key in ("lovelace", "ada"):
            continue
        if isinstance(val, dict):
            for name, qty in val.items():
                q = abs(int(qty)) if qty else 0
                if q > best[2]:
                    best = (key, name, q)
        else:
            q = abs(int(val)) if val else 0
            if q > best[2]:
                best = (key, "", q)
    return best


def _hex_to_ascii(hex_str: str) -> str:
    """Decode an asset-name hex string to 7-bit ASCII printable, else empty.

    The restriction is deliberate: this string is rendered in operator UI
    alongside the policy id. A name containing non-ASCII characters could
    carry confusables / zero-width characters that visually impersonate a
    legitimate ticker. Surfacing only the safe ASCII form forces operators
    to look at the hex whenever the name is non-trivial, which is the right
    default on a risk surface. The hex is always shown beside it.
    """
    if not hex_str:
        return ""
    try:
        decoded = bytes.fromhex(hex_str).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    if all(32 <= ord(c) < 127 for c in decoded):
        return decoded
    return ""


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
            _, token_count = feat_mod.count_assets(value)
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
        best_evidence: Dict[str, Any] = {}

        for out in outputs:
            addr = out.get("address", "")
            if not feat_mod.is_script_address(addr):
                continue
            value = out.get("value", {})
            if not isinstance(value, dict):
                continue
            _, token_count = feat_mod.count_assets(value)
            if token_count == 0 or token_count > 2:
                continue

            result = self._score_utxo(out, addr, network)
            if result.score > best_score:
                best_score = result.score
                best_sub = result.sub_scores
                best_reasons = result.reasons
                best_bl_source = result.baseline_source
                best_evidence = result.evidence

        return ScorerResult(
            score=best_score,
            sub_scores=best_sub,
            reasons=best_reasons,
            baseline_source=best_bl_source,
            evidence=best_evidence,
        )

    def _score_utxo(
        self, output: Dict, address: str, network: str,
    ) -> ScorerResult:
        value = output.get("value", {})
        if not isinstance(value, dict):
            value = {"lovelace": 0}

        ada_amount = feat_mod.extract_lovelace(value)
        value_cbor = feat_mod._estimate_value_cbor_bytes(value)
        qty_digits = _max_quantity_digits(value)
        policy_id, asset_name_hex, max_quantity = _primary_asset(value)
        asset_name_ascii = _hex_to_ascii(asset_name_hex)

        # quantity_digits: per-policy baseline (Polimi Section 4.2.3).
        # When the UTxO has no policy id, skip baseline lookup and jump
        # straight to the bootstrap anchor.
        if policy_id:
            p50_qd, p99_qd, bl1 = _resolve(
                "quantity_digits", "per_policy", policy_id, network,
                _BOOT, "quantity_digits",
            )
        else:
            p50_qd, p99_qd, bl1 = (*_anchor(_BOOT, "quantity_digits"), "bootstrap")

        p50_cb, p99_cb, _ = _resolve(
            "value_cbor_bytes", "per_script", address, network,
            _BOOT, "value_cbor_bytes",
        )
        p50_ada, p99_ada, _ = _resolve(
            "ada_amount", "per_script", address, network,
            _BOOT, "ada_amount",
        )
        bl_source = bl1

        # Sub-scores
        s_digits = normalise(qty_digits, p50=p50_qd, p99=p99_qd)
        s_bytes = normalise(value_cbor, p50=p50_cb, p99=p99_cb)
        s_ada = normalise_inverted(ada_amount, p50=p50_ada, p99=p99_ada)
        s_recurrence = 0.0  # requires entity clustering (deferred to mainnet)

        raw = (
            float(_W["digits"]) * s_digits
            + float(_W["bytes"]) * s_bytes
            + float(_W["ada_inv"]) * s_ada
            + float(_W["recurrence"]) * s_recurrence
        )
        final = finalise_score(raw)

        # Primary-signal gate: a quantity at or below the median normal supply
        # (digits sub-score ~0) is not a large-value outlier, regardless of how
        # lean its ADA or how heavy its Value CBOR. Hold such findings to the
        # top of Low so the secondary axes cannot alone raise a normal min-ADA
        # token UTxO to Moderate. A genuine overflow toward the int64 ceiling
        # has a high digits sub-score and is unaffected.
        if s_digits < _MIN_DIGITS_SUBSCORE:
            final = min(final, BAND_LOW_MAX)

        reasons = []
        if s_digits > _REASON_T:
            reasons.append("extreme_quantity_digits")
        if s_bytes > _REASON_T:
            reasons.append("high_value_cbor_for_few_assets")
        if s_ada > _REASON_T:
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
            evidence={
                "policy_id": policy_id,
                "asset_name_hex": asset_name_hex,
                "asset_name_ascii": asset_name_ascii,
                "max_quantity_raw": int(max_quantity),
                "quantity_digits_raw": int(qty_digits),
                "value_cbor_bytes_raw": int(value_cbor),
                "lovelace_amount": int(ada_amount),
                "target_script_address": address,
            },
        )
