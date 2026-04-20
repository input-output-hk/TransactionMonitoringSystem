"""Multiple Satisfaction attack scorer (Class 4).

Detects transactions that consume multiple UTxOs from the same script address
with structural properties consistent with a validator vulnerability where a
single satisfying argument covers multiple inputs simultaneously.

`redeemer_input_ratio` is deliberately not a scoring feature: the Cardano
ledger enforces `redeemers_count == n_script_inputs`
(dom txrdmrs ≡ᵉ scriptRdrptrs), so the ratio is structurally constant at 1.0
for all valid on-chain txs and carries no discriminative information. The
vulnerability is semantic and is not observable through redeemer counts.

Sub-scores (Polimi §4.4.3), all per-script baselined:

  net_value_out_of_script    (0.42): per-script baseline
  exunits_per_script_input   (0.28): inverted, per-script baseline
  n_inputs_same_script       (0.16): per-script baseline
  sender_recurrence          (0.14): per-script baseline (DBSCAN deferred, §5.1)

Allowlist behaviour (§4.4.4): transactions interacting with known batch
validators (DEX settlement, staking consolidation, prediction-market
resolution) have `s_extraction` weight set to 0 and redistributed across
`s_inputs` and `s_recurrence`, instead of bypassing the scorer entirely.
"""

import logging
from typing import Any, Dict, List, Tuple

from app.analysis.normalise import normalise, normalise_inverted, resolve_baseline
from app.analysis.scorers.base import BaseScorer, ScorerResult
from app.analysis import features as feat_mod

logger = logging.getLogger(__name__)

EPSILON = 1e-6

# Weights (Polimi §4.4.3)
_W_EXTRACTION = 0.42
_W_EXUNITS = 0.28
_W_INPUTS = 0.16
_W_RECURRENCE = 0.14

# Bootstrap anchors used when resolve_baseline() returns "missing".
_BOOT_NET_VALUE = (5_000_000.0, 500_000_000.0)
_BOOT_EXUNITS = (100_000.0, 10_000_000.0)
_BOOT_N_INPUTS = (2.0, 10.0)
_BOOT_RECURRENCE = (0.0, 1.0)

# Known batch-processing / resolution script prefixes. Transactions interacting
# with these scripts have s_extraction neutralised and its weight redistributed
# (§4.4.4). Add prediction-market resolution contracts here as they are
# discovered.
_ALLOWLISTED_SCRIPT_PREFIXES: List[str] = [
    "addr1w9zsmyfc5tg49ng9gqaetm8qheyheemxakq47x7qfwnq5wq",  # SundaeSwap v3 batch
    "addr1z8snz7c4974vzdpxu65ruphl3zjdvtxw8strf2c2tmqnxz",   # Minswap v2 batch
    "addr1wyx22z2s4kasd3w976pnjf9xdty88epjqfvgkmfnfpcsgh",   # WingRiders batch
]


def _group_inputs_by_script(raw_data: Dict) -> Dict[str, List[Dict]]:
    """Group transaction inputs by script address."""
    groups: Dict[str, List[Dict]] = {}
    for inp in raw_data.get("inputs", []):
        addr = inp.get("address", "")
        if feat_mod.is_script_address(addr):
            groups.setdefault(addr, []).append(inp)
    return groups


def _extract_lovelace(val: Any) -> int:
    """Extract lovelace from Ogmios v5 `{"lovelace": N}` or v6 `{"ada": {"lovelace": N}}`."""
    if isinstance(val, dict):
        ada = val.get("ada")
        if isinstance(ada, dict):
            return int(ada.get("lovelace", 0))
        return int(val.get("lovelace", 0))
    if val:
        return int(val)
    return 0


def _compute_net_value_out(
    inputs: List[Dict], outputs: List[Dict], script_addr: str,
) -> int:
    """Net lovelace extraction: Σ(inputs from script) − Σ(outputs to script)."""
    value_in = sum(
        _extract_lovelace(inp.get("value"))
        for inp in inputs if inp.get("address", "") == script_addr
    )
    value_out = sum(
        _extract_lovelace(out.get("value"))
        for out in outputs if out.get("address", "") == script_addr
    )
    return max(0, value_in - value_out)


def _total_exunits_cpu(raw_data: Dict) -> int:
    """Sum CPU execution units across all redeemers (v5 list or v6 dict/list)."""
    redeemers = raw_data.get("redeemers")
    if not redeemers:
        return 0
    items = redeemers.values() if isinstance(redeemers, dict) else redeemers
    total = 0
    for r in items:
        budget = r.get("executionUnits", r.get("budget", {}))
        total += int(budget.get("cpu", budget.get("steps", 0)))
    return total


def _is_allowlisted(script_addr: str) -> bool:
    return any(script_addr.startswith(p) for p in _ALLOWLISTED_SCRIPT_PREFIXES)


def _reweight_without_extraction() -> Tuple[float, float, float, float]:
    """Redistribute _W_EXTRACTION proportionally to _W_INPUTS and _W_RECURRENCE.

    Returns (w_extraction, w_exunits, w_inputs, w_recurrence) with w_extraction
    forced to 0 and its mass spread across inputs/recurrence by their ratio.
    """
    surviving = _W_INPUTS + _W_RECURRENCE
    bonus_inputs = _W_EXTRACTION * (_W_INPUTS / surviving)
    bonus_recurrence = _W_EXTRACTION * (_W_RECURRENCE / surviving)
    return (
        0.0,
        _W_EXUNITS,
        _W_INPUTS + bonus_inputs,
        _W_RECURRENCE + bonus_recurrence,
    )


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
        outputs = raw_data.get("outputs", [])
        total_cpu = _total_exunits_cpu(raw_data)
        sender_recurrence = float(features.get("sender_recurrence", 0.0) or 0.0)

        groups = _group_inputs_by_script(raw_data)

        best: ScorerResult = ScorerResult()

        for script_addr, inps in groups.items():
            n_inputs = len(inps)
            if n_inputs < 2:
                continue

            result = self._score_script(
                script_addr, n_inputs, total_cpu,
                raw_data, outputs, sender_recurrence,
            )
            if result.score > best.score:
                best = result

        return best

    def _score_script(
        self,
        script_addr: str,
        n_inputs: int,
        total_cpu: int,
        raw_data: Dict,
        outputs: List[Dict],
        sender_recurrence: float,
    ) -> ScorerResult:
        net_value = _compute_net_value_out(
            raw_data.get("inputs", []), outputs, script_addr,
        )
        exunits_per_input = total_cpu / (n_inputs + EPSILON)

        # Per-script baselines with bootstrap fallbacks.
        p50_nv, p99_nv, bl_nv = resolve_baseline(
            "net_value_out_of_script", "per_script", script_addr,
        )
        if bl_nv == "missing":
            p50_nv, p99_nv = _BOOT_NET_VALUE

        p50_ex, p99_ex, bl_ex = resolve_baseline(
            "exunits_per_script_input", "per_script", script_addr,
        )
        if bl_ex == "missing":
            p50_ex, p99_ex = _BOOT_EXUNITS

        p50_ni, p99_ni, bl_ni = resolve_baseline(
            "n_inputs_same_script", "per_script", script_addr,
        )
        if bl_ni == "missing":
            p50_ni, p99_ni = _BOOT_N_INPUTS

        p50_rc, p99_rc, bl_rc = resolve_baseline(
            "sender_recurrence", "per_script", script_addr,
        )
        if bl_rc == "missing":
            p50_rc, p99_rc = _BOOT_RECURRENCE

        # Sub-scores.
        s_extraction = normalise(net_value, p50=p50_nv, p99=p99_nv)
        s_exunits_inv = normalise_inverted(exunits_per_input, p50=p50_ex, p99=p99_ex)
        s_inputs = normalise(float(n_inputs), p50=p50_ni, p99=p99_ni)
        s_recurrence = normalise(sender_recurrence, p50=p50_rc, p99=p99_rc)

        # Allowlisted scripts: neutralise s_extraction and redistribute its weight.
        allowlisted = _is_allowlisted(script_addr)
        if allowlisted:
            w_ex, w_eu, w_ni, w_rc = _reweight_without_extraction()
            s_extraction = 0.0
        else:
            w_ex, w_eu, w_ni, w_rc = (
                _W_EXTRACTION, _W_EXUNITS, _W_INPUTS, _W_RECURRENCE,
            )

        raw = (
            w_ex * s_extraction
            + w_eu * s_exunits_inv
            + w_ni * s_inputs
            + w_rc * s_recurrence
        )
        final = round(max(0.0, min(1.0, raw)) * 100, 2)

        # The baseline source reported is the "most specific tier actually used"
        # across the four features. Prefer per_script > per_policy > global > missing.
        bl_source = _dominant_source([bl_nv, bl_ex, bl_ni, bl_rc])

        reasons = []
        if s_extraction > 0.5:
            reasons.append("large_net_value_extraction")
        if s_exunits_inv > 0.5:
            reasons.append("low_exunits_per_input")
        if s_inputs > 0.5:
            reasons.append("high_n_inputs_same_script")
        if allowlisted:
            reasons.append("allowlisted_batch_script")

        return ScorerResult(
            score=final,
            sub_scores={
                "s_extraction": round(s_extraction, 4),
                "s_exunits_inv": round(s_exunits_inv, 4),
                "s_inputs": round(s_inputs, 4),
                "s_recurrence": round(s_recurrence, 4),
                "n_inputs_same_script": float(n_inputs),
            },
            reasons=reasons,
            baseline_source=bl_source,
        )


def _dominant_source(sources: List[str]) -> str:
    """Return the most specific baseline tier used.

    Priority: per_script > per_policy > global > missing.
    """
    order = ["per_script", "per_policy", "global", "missing"]
    for tier in order:
        if tier in sources:
            return tier
    return "missing"
