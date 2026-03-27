"""Multiple Satisfaction attack scorer (Class 4).

Detects transactions that consume multiple UTxOs from the same script address
while providing fewer redeemers than inputs: the structural fingerprint of a
validator vulnerability where a single spending condition satisfies multiple
inputs simultaneously.

The key signal is redeemer_input_ratio: under correct Cardano semantics each
script input requires its own redeemer, yielding a ratio of 1.0.  A ratio
significantly below 1.0 indicates potential redeemer reuse.

Sub-scores (Polimi Section 4.4.3, adjusted):
  redeemer_input_ratio  (0.30): inverted, fixed anchors p50=0.0 p99=0.70
  net_value_out_of_script (0.20): per-script baseline
  exunits_per_input     (0.20): inverted, per-script baseline
  full_drain            (0.20): 1.0 if all script value extracted, nothing returned
  sender_recurrence     (0.10): requires entity clustering (deferred to mainnet)
"""

import logging
from typing import Any, Dict, List

from app.analysis.normalise import normalise, normalise_inverted, resolve_baseline
from app.analysis.scorers.base import BaseScorer, ScorerResult
from app.analysis import features as feat_mod

logger = logging.getLogger(__name__)

# Fixed anchors for 1 - redeemer_input_ratio (Polimi Section 5.4)
_RIR_P50 = 0.0
_RIR_P99 = 0.70

EPSILON = 1e-6


def _group_inputs_by_script(raw_data: Dict) -> Dict[str, List[Dict]]:
    """Group transaction inputs by script address.

    Returns {address: [input_dicts]} for script addresses only.
    """
    groups: Dict[str, List[Dict]] = {}
    for inp in raw_data.get("inputs", []):
        addr = inp.get("address", "")
        if feat_mod.is_script_address(addr):
            groups.setdefault(addr, []).append(inp)
    return groups


def _extract_lovelace(val: Any) -> int:
    """Extract lovelace from various Ogmios value formats.

    Ogmios v5: {"lovelace": N}
    Ogmios v6: {"ada": {"lovelace": N}}
    Scalar: N
    """
    if isinstance(val, dict):
        # Ogmios v6: {"ada": {"lovelace": N}}
        ada = val.get("ada")
        if isinstance(ada, dict):
            return int(ada.get("lovelace", 0))
        # Ogmios v5: {"lovelace": N}
        return int(val.get("lovelace", 0))
    if val:
        return int(val)
    return 0


def _compute_net_value_out(
    inputs: List[Dict], outputs: List[Dict], script_addr: str,
) -> int:
    """Compute net ADA extraction from a script address.

    Sum of input values from script_addr minus sum of output values sent
    back to script_addr.
    """
    value_in = sum(
        _extract_lovelace(inp.get("value"))
        for inp in inputs if inp.get("address", "") == script_addr
    )
    value_out = sum(
        _extract_lovelace(out.get("value"))
        for out in outputs if out.get("address", "") == script_addr
    )
    return max(0, value_in - value_out)


def _count_spending_redeemers_for_script(
    raw_data: Dict, script_addr: str, n_inputs_before: int,
) -> int:
    """Count spending redeemers scoped to a specific script address.

    Cardano redeemers are indexed by input position.  We identify which
    input indices belong to the target script and count how many of those
    indices have a corresponding spending redeemer.

    When redeemer indexing cannot be resolved (e.g., Ogmios v5 list format
    without explicit indices), falls back to global spending redeemer count
    as an approximation.
    """
    redeemers = raw_data.get("redeemers")
    if not redeemers:
        return 0

    # Build set of input indices belonging to the target script
    inputs = raw_data.get("inputs", [])
    script_indices = set()
    for idx, inp in enumerate(inputs):
        if inp.get("address", "") == script_addr:
            script_indices.add(idx)

    if isinstance(redeemers, dict):
        # Ogmios v6 format: "spend:N" keys where N is the input index
        count = 0
        for key in redeemers:
            if key.startswith("spend:"):
                try:
                    idx = int(key.split(":")[1])
                    if idx in script_indices:
                        count += 1
                except (ValueError, IndexError):
                    pass
        return count

    if isinstance(redeemers, list):
        # Ogmios v6 list format: [{"validator": {"index": N, "purpose": "spend"}, ...}]
        # Ogmios v5 list format: [{"purpose": "spend", "index": N, ...}]
        count = 0
        for r in redeemers:
            validator = r.get("validator", {})
            purpose = (
                validator.get("purpose", "")
                or r.get("purpose", r.get("tag", ""))
            ).lower()
            if purpose.startswith("spend"):
                idx = validator.get("index", r.get("index", -1))
                if idx in script_indices:
                    count += 1
        # If no indices matched (missing index fields), fall back to global
        if count == 0 and any(
            (r.get("validator", {}).get("purpose", "")
             or r.get("purpose", r.get("tag", ""))).lower().startswith("spend")
            for r in redeemers
        ):
            return sum(
                1 for r in redeemers
                if (r.get("validator", {}).get("purpose", "")
                    or r.get("purpose", r.get("tag", ""))).lower().startswith("spend")
            )
        return count

    return 0


# Known batch-processing script address prefixes (withdraw-zero, DEX settlement)
# Transactions interacting with these scripts bypass redeemer_input_ratio scoring
_BATCH_SCRIPT_ALLOWLIST: List[str] = [
    # SundaeSwap v3 order batch validator
    "addr1w9zsmyfc5tg49ng9gqaetm8qheyheemxakq47x7qfwnq5wq",
    # Minswap v2 batch validator
    "addr1z8snz7c4974vzdpxu65ruphl3zjdvtxw8strf2c2tmqnxz",
    # WingRiders batch settlement
    "addr1wyx22z2s4kasd3w976pnjf9xdty88epjqfvgkmfnfpcsgh",
]


def _total_exunits_cpu(raw_data: Dict) -> int:
    """Sum CPU execution units across all redeemers."""
    redeemers = raw_data.get("redeemers")
    if not redeemers:
        return 0
    total = 0
    items = redeemers.values() if isinstance(redeemers, dict) else redeemers
    for r in items:
        budget = r.get("executionUnits", r.get("budget", {}))
        total += int(budget.get("cpu", budget.get("steps", 0)))
    return total


class MultipleSatScorer(BaseScorer):
    name = "multiple_sat"

    def gate(self, features: Dict[str, Any]) -> bool:
        """At least 2 inputs from the same script address."""
        raw_data = features.get("raw_data")
        if not raw_data or not isinstance(raw_data, dict):
            return False
        groups = _group_inputs_by_script(raw_data)
        return any(len(inps) >= 2 for inps in groups.values())

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        raw_data = features.get("raw_data", {})
        network = features.get("network", "")
        outputs = raw_data.get("outputs", [])

        groups = _group_inputs_by_script(raw_data)
        total_cpu = _total_exunits_cpu(raw_data)

        best_score = 0.0
        best_sub = {}
        best_reasons = []
        best_bl_source = "missing"

        for script_addr, inps in groups.items():
            n_inputs = len(inps)
            if n_inputs < 2:
                continue

            # Skip allowlisted batch-processing scripts
            if any(script_addr.startswith(p) for p in _BATCH_SCRIPT_ALLOWLIST):
                continue

            # Scope redeemer count to this specific script
            script_redeemers = _count_spending_redeemers_for_script(
                raw_data, script_addr, n_inputs,
            )

            result = self._score_script(
                script_addr, n_inputs, script_redeemers,
                total_cpu, raw_data, outputs, network,
            )
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

    def _score_script(
        self,
        script_addr: str,
        n_inputs: int,
        spending_redeemers: int,
        total_cpu: int,
        raw_data: Dict,
        outputs: List[Dict],
        network: str,
    ) -> ScorerResult:
        # Derived features
        redeemer_input_ratio = spending_redeemers / (n_inputs + EPSILON)
        inverted_rir = 1.0 - redeemer_input_ratio

        net_value = _compute_net_value_out(
            raw_data.get("inputs", []), outputs, script_addr,
        )

        exunits_per_input = total_cpu / (n_inputs + EPSILON)

        # Sub-score 1: redeemer_input_ratio inverted (fixed anchors)
        s_redeemer = normalise(inverted_rir, p50=_RIR_P50, p99=_RIR_P99)

        # Sub-score 2: net_value_out_of_script (per-script baseline)
        p50_nv, p99_nv, bl1 = resolve_baseline(
            "net_value_out_of_script", "per_script", script_addr,
        )
        if bl1 == "missing":
            p50_nv, p99_nv = 5_000_000.0, 500_000_000.0  # bootstrap
        s_extraction = normalise(net_value, p50=p50_nv, p99=p99_nv)

        # Sub-score 3: exunits per input inverted (per-script baseline)
        p50_ex, p99_ex, bl2 = resolve_baseline(
            "exunits_per_script_input", "per_script", script_addr,
        )
        if bl2 == "missing":
            p50_ex, p99_ex = 100_000.0, 10_000_000.0  # bootstrap
        s_exunits = normalise_inverted(exunits_per_input, p50=p50_ex, p99=p99_ex)

        # Sub-score 4: full drain detection
        # If all value is extracted from script (nothing returned), this is a
        # strong structural signal regardless of redeemer ratio or amount.
        value_in = sum(
            _extract_lovelace(inp.get("value"))
            for inp in raw_data.get("inputs", [])
            if inp.get("address", "") == script_addr
        )
        value_returned = sum(
            _extract_lovelace(out.get("value"))
            for out in outputs
            if out.get("address", "") == script_addr
        )
        # Full drain: script had value and nothing came back
        s_full_drain = 1.0 if (value_in > 0 and value_returned == 0) else 0.0

        # Sub-score 5: sender recurrence (requires entity clustering)
        s_recurrence = 0.0

        bl_source = bl1 if bl1 != "missing" else "bootstrap"

        # Weights: redeemer_ratio 0.30, extraction 0.20, exunits 0.20,
        #          full_drain 0.20, recurrence 0.10
        raw = (
            0.30 * s_redeemer
            + 0.20 * s_extraction
            + 0.20 * s_exunits
            + 0.20 * s_full_drain
            + 0.10 * s_recurrence
        )
        final = round(max(0.0, min(1.0, raw)) * 100, 2)

        reasons = []
        if s_redeemer > 0.5:
            reasons.append("low_redeemer_input_ratio")
        if s_extraction > 0.5:
            reasons.append("large_net_value_extraction")
        if s_exunits > 0.5:
            reasons.append("low_exunits_per_input")
        if s_full_drain > 0.5:
            reasons.append("full_drain_from_script")

        return ScorerResult(
            score=final,
            sub_scores={
                "redeemer_input_ratio_inv": round(s_redeemer, 4),
                "net_value_extraction": round(s_extraction, 4),
                "exunits_per_input_inv": round(s_exunits, 4),
                "full_drain": round(s_full_drain, 4),
                "sender_recurrence": round(s_recurrence, 4),
                "n_inputs_same_script": n_inputs,
                "redeemer_input_ratio": round(redeemer_input_ratio, 4),
            },
            reasons=reasons,
            baseline_source=bl_source,
        )
