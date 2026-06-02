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

  net_value_out_of_script    : per-script baseline
  n_assets_out_of_script     : per-script baseline (Phase 1 extension, see below)
  exunits_per_script_input   : inverted, per-script baseline
  n_inputs_same_script       : per-script baseline
  sender_recurrence          : per-script baseline (DBSCAN deferred, §5.1)

Phase 1 extension to §4.4.3: ``s_extraction`` is value-agnostic. The Polimi
spec defines extraction in lovelace only, but the canonical real-world cases
(the 2021-22 NFT-marketplace double-satisfaction disclosures affecting
jpg.store, SpaceBudz, Genesis, Adapix, Martify) drain native assets while
the script's lovelace position barely moves. We compute both axes
independently against per-script baselines and take the max, so either
dimension can carry the signal without dilution. Both axes are reported in
``sub_scores`` for observability.

Allowlist behaviour (§4.4.4): transactions interacting with known batch
validators (DEX settlement, staking consolidation, prediction-market
resolution) have `s_extraction` weight set to 0 and redistributed across
`s_inputs` and `s_recurrence`, instead of bypassing the scorer entirely.
The allowlist is network-scoped: a preprod prefix never suppresses
mainnet alerts.

Band floor for confirmed structural exploitation: when the gate has fired
and ``s_exunits_inv`` saturates (low CPU per input → "lazy validator"
fingerprint), the final score is floored to at least the High band. The
spec's weighted average is biased toward value extraction, so a low-value
structural exploit can score in the Moderate band even when the
structural confirmation is strong; this floor lifts those into High so
operators triage on signal strength rather than dollar impact. Mirrors
the mechanism in ``front_running`` (where ``high_band_cap`` *caps* scores
when structural confirmation is weak). Allowlisted scripts are exempt.

Uniform-sweep guard: the floor is also suppressed when the tx
fingerprint is "owner sweeping their own script UTxOs" (many inputs with
identical spend redeemers and no value returned to the same script).
This is a UTxO consolidation, structurally distinct from
double-satisfaction (which has asymmetric satisfaction arguments). The
guard is config-gated under ``uniform_sweep_guard`` so each leg
(uniform-redeemer, no-return, min-inputs) can be loosened independently.

Extraction sanity gate: the floor additionally requires
``s_extraction > lazy_validator_extraction_min``. Double-satisfaction by
definition needs value to leave the script; state-machine contracts that
consume their own UTxOs and write state back have ``s_extraction = 0``
and are not exploits even when execution is cheap.

All tunable constants live in ``config/detection.yaml`` under the
``scorers.multiple_sat`` section.
"""

import logging
from functools import lru_cache
from typing import Any, Dict, List, Tuple

from app.analysis.normalise import (
    BAND_HIGH_THRESHOLD,
    BAND_MODERATE_MAX,
    normalise,
    normalise_inverted,
)
from app.analysis.scorer_config import (
    get as _get_cfg,
    load_network_map as _load_network_map,
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import BaseScorer, ScorerResult, finalise_score
from app.analysis import features as feat_mod
from app.analysis.features import extract_lovelace as _extract_lovelace

logger = logging.getLogger(__name__)

EPSILON = 1e-6

_CFG = _get_cfg("multiple_sat")
_W = _CFG["weights"]
_BOOT = _CFG["bootstrap_anchors"]

# The VALUE-extraction axis (net_value / n_assets out of the script) resolves
# per-script then drops straight to the bootstrap anchor, NEVER the global tier.
# The global distribution of value/assets leaving a script is dominated by
# legitimate high-volume asset-movers (DEX/marketplace batchers), so a global
# baseline would learn "extracting 2+ assets is normal" and de-sensitise
# detection on the rare/novel scripts where one-shot double-satisfaction
# exploits live (the CTF-01 anchor extracts 2 assets on a 3-tx script).
# per_script -> bootstrap keeps established contracts judged against their own
# norm while rare scripts stay on the conservative default.
#
# This is applied ONLY to the value axis. exunits_per_script_input feeds the
# INVERTED lazy-validator signal: "lazy" means near-zero CPU in absolute terms,
# so it must stay on the absolute bootstrap. A per-script exunits baseline would
# make a script that consistently does heavy work (its median CPU) look
# maximally lazy against itself and spuriously floor it to High. n_inputs is
# likewise left on the default resolution.
_PER_SCRIPT_ONLY = ("per_script",)


_ALLOWLIST: Dict[str, Tuple[str, ...]] = _load_network_map(
    _CFG.get("allowlist_prefixes"),
    scorer="multiple_sat",
    field="allowlist_prefixes",
    collect=tuple,
)
_REASON_T: float = float(_CFG["reason_threshold"])
_LAZY_VALIDATOR_THRESHOLD: float = float(_CFG["lazy_validator_threshold"])
_LAZY_VALIDATOR_FLOOR: float = float(_CFG["lazy_validator_floor"])
_LAZY_VALIDATOR_EXTRACTION_MIN: float = float(_CFG["lazy_validator_extraction_min"])
_SWEEP_GUARD = _CFG["uniform_sweep_guard"]
_SWEEP_GUARD_ENABLED: bool = bool(_SWEEP_GUARD["enabled"])
_SWEEP_REQ_UNIFORM_RED: bool = bool(_SWEEP_GUARD["require_uniform_redeemer"])
_SWEEP_REQ_NO_RETURN: bool = bool(_SWEEP_GUARD["require_no_script_return"])
_SWEEP_MIN_INPUTS: int = int(_SWEEP_GUARD["min_inputs"])

# The floor's purpose is to guarantee the band lands at High; if config
# drifts below the High threshold the docstring's promise breaks. Fail
# loud at import. Use an explicit raise rather than ``assert`` so the
# check survives ``python -O`` / ``PYTHONOPTIMIZE``.
if _LAZY_VALIDATOR_FLOOR < BAND_HIGH_THRESHOLD:
    raise RuntimeError(
        f"multiple_sat.lazy_validator_floor={_LAZY_VALIDATOR_FLOOR} is below "
        f"normalise.BAND_HIGH_THRESHOLD={BAND_HIGH_THRESHOLD}; floor would not "
        f"reach the High band. Either raise the floor in detection.yaml or "
        f"adjust the band threshold in normalise.py."
    )


_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_INVERSE = {c: i for i, c in enumerate(_BECH32_CHARSET)}
_BECH32_CHECKSUM_LEN = 6
_BECH32_BITS_PER_CHAR = 5
_PAYMENT_CRED_BYTES = 28  # CIP-19: Blake2b-224 hash size, payment + stake creds


@lru_cache(maxsize=4096)
def _payment_credential(addr: str) -> str:
    """Return a stable per-script-hash key for a Shelley script address.

    Two addresses sharing the same payment credential (script hash) but
    differing in stake credential must group together: a validator
    vulnerability can be exploited by spending multiple UTxOs at the same
    script with distinct stake credentials, putting them at distinct
    ``address`` strings but the same script. Grouping by raw address
    misses the attack (canonical purchase-offer double-satisfaction shape).

    Decodes the bech32 data, drops the network header byte, and returns the
    first 28 bytes (= payment credential hash) as hex. The 6-char bech32
    checksum at the tail is *stripped without validation*: callers must not
    rely on a successful decode meaning the address is well-formed. On any
    structural failure falls back to the raw address so legacy code paths
    keep working.

    Cached because each tx triggers up to 3·(N_inputs + N_outputs) calls
    (grouping + lovelace flow + asset flow) across the same address set.
    """
    if not addr or "1" not in addr:
        return addr
    try:
        data_part = addr.rsplit("1", 1)[1].lower()
        if len(data_part) <= _BECH32_CHECKSUM_LEN:
            return addr
        # Strip the trailing checksum without validating it; callers must
        # not rely on a successful decode meaning the address is well-formed.
        data_part = data_part[:-_BECH32_CHECKSUM_LEN]
        bits: List[int] = []
        for c in data_part:
            v = _BECH32_INVERSE.get(c)
            if v is None:
                return addr
            # Unpack 5-bit bech32 group MSB-first.
            for shift in range(_BECH32_BITS_PER_CHAR - 1, -1, -1):
                bits.append((v >> shift) & 1)
        # Layout: 1 header byte + 28-byte payment cred + (optional stake cred).
        header_bits = 8
        payment_cred_bits = _PAYMENT_CRED_BYTES * 8
        if len(bits) < header_bits + payment_cred_bits:
            return addr
        out = bytearray()
        for i in range(header_bits, header_bits + payment_cred_bits, 8):
            byte = 0
            for b in bits[i : i + 8]:
                byte = (byte << 1) | b
            out.append(byte)
        return out.hex()
    except Exception:
        return addr


def _group_inputs_by_script(raw_data: Dict) -> Dict[str, List[Dict]]:
    """Group transaction inputs by script payment credential.

    Keyed by payment credential (not full address) so that script UTxOs at
    the same validator with different stake credentials group together. See
    :func:`_payment_credential`.
    """
    groups: Dict[str, List[Dict]] = {}
    for inp in raw_data.get("inputs", []):
        addr = inp.get("address", "")
        if feat_mod.is_script_address(addr):
            groups.setdefault(_payment_credential(addr), []).append(inp)
    return groups


def _compute_lovelace_flow(
    inputs: List[Dict], outputs: List[Dict], script_key: str,
) -> Tuple[int, int]:
    """Return ``(lovelace_in_at_script, lovelace_out_at_script)``.

    ``script_key`` is a payment credential (see :func:`_payment_credential`).
    Callers that only need the net extraction can take ``max(0, in - out)``.
    """
    value_in = sum(
        _extract_lovelace(inp.get("value"))
        for inp in inputs if _payment_credential(inp.get("address", "")) == script_key
    )
    value_out = sum(
        _extract_lovelace(out.get("value"))
        for out in outputs if _payment_credential(out.get("address", "")) == script_key
    )
    return value_in, value_out


def _compute_net_value_out(
    inputs: List[Dict], outputs: List[Dict], script_key: str,
) -> int:
    """Net lovelace extraction: ``max(0, in - out)`` over the script.

    Kept as a thin wrapper over :func:`_compute_lovelace_flow` so existing
    call sites (and tests) don't have to change.
    """
    value_in, value_out = _compute_lovelace_flow(inputs, outputs, script_key)
    return max(0, value_in - value_out)


def _iter_assets(val: Any):
    """Yield ((policy_id, asset_name), qty) pairs from an Ogmios value dict.

    Skips the lovelace component. Handles both v5 (`{"lovelace": N, policy: {asset: qty}}`)
    and v6 (`{"ada": {"lovelace": N}, policy: {asset: qty}}`) shapes.
    """
    if not isinstance(val, dict):
        return
    for policy, inner in val.items():
        if policy in ("ada", "lovelace"):
            continue
        if not isinstance(inner, dict):
            continue
        for asset_name, qty in inner.items():
            try:
                yield (policy, asset_name), int(qty)
            except (TypeError, ValueError):
                continue


def _compute_n_assets_out(
    inputs: List[Dict], outputs: List[Dict], script_key: str,
) -> int:
    """Count of distinct native-asset ``(policy, name)`` pairs with positive
    net flow out of the script address.

    For each pair: ``qty_in_at_script − qty_out_at_script``; return the number
    of pairs whose net is strictly > 0. The metric is *pair count*, not unit
    count: a partial extraction of 50 fungible-token units of one asset
    registers as 1, the same as a single NFT. This matches the canonical
    NFT-marketplace double-satisfaction shape (N NFTs leaving = N pairs
    leaving) and is robust to fungible-vs-NFT differences.
    """
    flow: Dict[Tuple[str, str], int] = {}
    for inp in inputs:
        if _payment_credential(inp.get("address", "")) != script_key:
            continue
        for key, qty in _iter_assets(inp.get("value")):
            flow[key] = flow.get(key, 0) + qty
    for out in outputs:
        if _payment_credential(out.get("address", "")) != script_key:
            continue
        for key, qty in _iter_assets(out.get("value")):
            flow[key] = flow.get(key, 0) - qty
    return sum(1 for net in flow.values() if net > 0)


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


def _is_allowlisted(script_addr: str, network: str) -> bool:
    prefixes = _ALLOWLIST.get(network, ())
    return any(script_addr.startswith(p) for p in prefixes)


_PAYMENT_CRED_HEX_LEN = _PAYMENT_CRED_BYTES * 2
_HEX_CHARS = frozenset("0123456789abcdef")


def _is_decoded_payment_credential(script_key: str) -> bool:
    """True when ``script_key`` is a well-formed 56-char hex payment cred.

    ``_payment_credential`` falls back to the raw input address on decode
    failure. Predicates that compare credentials across inputs and outputs
    cannot be trusted in that case (the raw fallback never matches a
    properly-decoded output's credential), so callers must gate on this.
    """
    return (
        len(script_key) == _PAYMENT_CRED_HEX_LEN
        and all(c in _HEX_CHARS for c in script_key)
    )


def _spend_redeemer_payloads(raw_data: Dict) -> List[str]:
    """Return the list of spend-purpose redeemer payloads in the tx.

    Both Ogmios v5 (list) and v6 (dict-or-list) shapes are handled. Entries
    without a recognisable spend purpose are skipped, matching the way
    multiple-satisfaction can only be evaluated against script spends.
    """
    redeemers = raw_data.get("redeemers")
    if not redeemers:
        return []
    items = redeemers.values() if isinstance(redeemers, dict) else redeemers
    payloads: List[str] = []
    for r in items:
        validator = r.get("validator") or {}
        purpose = validator.get("purpose") or r.get("purpose")
        if purpose != "spend":
            continue
        payload = r.get("redeemer")
        if isinstance(payload, str):
            payloads.append(payload)
    return payloads


def _is_uniform_sweep(
    script_key: str,
    n_inputs: int,
    outputs: List[Dict],
    spend_redeemer_payloads: List[str],
) -> bool:
    """Owner-sweep fingerprint: many script inputs, identical spend
    redeemers, no value returned to the same script.

    All three predicates are individually weak; together they describe a
    UTxO consolidation rather than a double-satisfaction exploit. Each leg
    is independently gated by config so operators can loosen the guard if
    a real attack happens to share the shape.

    ``spend_redeemer_payloads`` is precomputed once per tx by the caller;
    recomputing it per script group would re-walk the same redeemer list
    for every group in a multi-script tx.

    Refuses to suppress when ``script_key`` is not a well-formed payment
    credential: the no-return predicate relies on credential equality
    between inputs (the group's key) and outputs (computed afresh), which
    silently mismatches when bech32 decode falls back to the raw address.
    """
    if not _SWEEP_GUARD_ENABLED:
        return False
    if n_inputs < _SWEEP_MIN_INPUTS:
        return False
    if _SWEEP_REQ_NO_RETURN and not _is_decoded_payment_credential(script_key):
        return False
    if _SWEEP_REQ_UNIFORM_RED:
        # Need at least as many spend redeemers as inputs in this group
        # (the ledger guarantees one redeemer per script input across the
        # whole tx; below this count something exotic is happening and we
        # decline to suppress).
        if (
            len(spend_redeemer_payloads) < n_inputs
            or len(set(spend_redeemer_payloads)) != 1
        ):
            return False
    if _SWEEP_REQ_NO_RETURN:
        for out in outputs:
            if _payment_credential(out.get("address", "")) == script_key:
                return False
    return True


def _reweight_without_extraction() -> Tuple[float, float, float, float]:
    """Redistribute the extraction weight proportionally to inputs and recurrence.

    Returns (w_extraction, w_exunits, w_inputs, w_recurrence) with w_extraction
    forced to 0 and its mass spread across inputs/recurrence by their ratio.
    """
    w_ex = float(_W["extraction"])
    w_eu = float(_W["exunits_inv"])
    w_ni = float(_W["inputs"])
    w_rc = float(_W["recurrence"])
    surviving = w_ni + w_rc
    bonus_inputs = w_ex * (w_ni / surviving)
    bonus_recurrence = w_ex * (w_rc / surviving)
    return (0.0, w_eu, w_ni + bonus_inputs, w_rc + bonus_recurrence)


class MultipleSatScorer(BaseScorer):
    name = "multiple_sat"

    def gate(self, features: Dict[str, Any]) -> bool:
        """At least 2 inputs from the same script address AND at least one
        spend redeemer in the tx.

        The 2-input threshold is definitional: below this count the concept of
        'multiple' satisfaction does not apply.

        The spend-redeemer requirement excludes native-script (multisig /
        timelock) addresses, which the ledger evaluates as declarative
        predicates per-input. Native scripts cannot be exploited via
        multiple-satisfaction by construction. See
        :func:`features.has_spend_redeemer` for the rationale; this gate
        is conservative for the rare mixed native+Plutus tx but eliminates
        the dominant false-positive class observed on preprod (native-script
        multisig wallets consolidating their own UTxOs).
        """
        raw_data = features.get("raw_data")
        if not raw_data or not isinstance(raw_data, dict):
            return False
        if not feat_mod.has_spend_redeemer(raw_data):
            return False
        groups = _group_inputs_by_script(raw_data)
        return any(len(inps) >= 2 for inps in groups.values())

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        raw_data = features.get("raw_data", {})
        outputs = raw_data.get("outputs", [])
        total_cpu = _total_exunits_cpu(raw_data)
        sender_recurrence = float(features.get("sender_recurrence", 0.0) or 0.0)
        network = features.get("network", "")

        groups = _group_inputs_by_script(raw_data)

        # Precompute once per tx: the uniform-sweep guard inspects the same
        # redeemer list for every script group, and the redeemer set only
        # depends on raw_data.
        spend_payloads = _spend_redeemer_payloads(raw_data)

        # Start at the "no finding" sentinel (-1) so a suppressed group
        # (no_finding, score -1) propagates as not-applicable instead of being
        # masked by a 0.0 default, and a tx with no qualifying script group also
        # yields -1 rather than a spurious applicable 0.0.
        best: ScorerResult = ScorerResult(score=-1.0)

        for script_key, inps in groups.items():
            n_inputs = len(inps)
            if n_inputs < 2:
                continue

            # Pick the first input's address as the representative for
            # baseline / allowlist lookups (these are still keyed by full
            # address). Group membership is already determined by
            # payment credential.
            representative_addr = inps[0].get("address", script_key)
            result = self._score_script(
                script_key, representative_addr, n_inputs, total_cpu,
                raw_data, outputs, sender_recurrence, network,
                spend_payloads,
            )
            # >= (not >) so a suppressed group's no_finding result (score -1)
            # replaces the -1 init and carries its observability sub_scores
            # through; a real finding (score >= 0) still wins over any -1.
            if result.score >= best.score:
                best = result

        return best

    def _score_script(
        self,
        script_key: str,
        representative_addr: str,
        n_inputs: int,
        total_cpu: int,
        raw_data: Dict,
        outputs: List[Dict],
        sender_recurrence: float,
        network: str,
        spend_redeemer_payloads: List[str],
    ) -> ScorerResult:
        inputs = raw_data.get("inputs", [])
        lovelace_in_at_script, lovelace_out_at_script = _compute_lovelace_flow(
            inputs, outputs, script_key,
        )
        net_value = max(0, lovelace_in_at_script - lovelace_out_at_script)
        n_assets_out = _compute_n_assets_out(inputs, outputs, script_key)
        exunits_per_input = total_cpu / (n_inputs + EPSILON)

        # Per-script baselines still keyed by full address; use the
        # representative address picked from the group.
        p50_nv, p99_nv, bl_nv = _resolve(
            "net_value_out_of_script", "per_script", representative_addr, network,
            _BOOT, "net_value_out_of_script", scope_types_allowed=_PER_SCRIPT_ONLY,
        )
        p50_na, p99_na, bl_na = _resolve(
            "n_assets_out_of_script", "per_script", representative_addr, network,
            _BOOT, "n_assets_out_of_script", scope_types_allowed=_PER_SCRIPT_ONLY,
        )
        # exunits + n_inputs keep the original per_script->global->bootstrap
        # resolution. exunits is an absolute lazy-validator signal (see
        # _PER_SCRIPT_ONLY) and must NOT be calibrated per-script.
        p50_ex, p99_ex, bl_ex = _resolve(
            "exunits_per_script_input", "per_script", representative_addr, network,
            _BOOT, "exunits_per_script_input",
        )
        p50_ni, p99_ni, bl_ni = _resolve(
            "n_inputs_same_script", "per_script", representative_addr, network,
            _BOOT, "n_inputs_same_script",
        )
        p50_rc, p99_rc, bl_rc = _resolve(
            "sender_recurrence", "per_script", representative_addr, network,
            _BOOT, "sender_recurrence",
        )

        # Extraction is value-agnostic: take the stronger of the lovelace and
        # the native-asset axis. NFT-marketplace double-sat exploits drain
        # native assets while the script's lovelace position barely moves;
        # taking max lets either axis carry the signal without dilution.
        s_extraction_lov = normalise(net_value, p50=p50_nv, p99=p99_nv)
        s_extraction_assets = normalise(float(n_assets_out), p50=p50_na, p99=p99_na)
        s_extraction = max(s_extraction_lov, s_extraction_assets)
        s_exunits_inv = normalise_inverted(exunits_per_input, p50=p50_ex, p99=p99_ex)
        s_inputs = normalise(float(n_inputs), p50=p50_ni, p99=p99_ni)
        s_recurrence = normalise(sender_recurrence, p50=p50_rc, p99=p99_rc)

        # Allowlisted scripts: neutralise s_extraction and redistribute its weight.
        # Check every address in the group, not just the representative: a known
        # batcher may publish UTxOs under multiple stake-cred variants, and the
        # group must be allowlisted if any variant is.
        group_addrs = {inp.get("address", "") for inp in inputs if _payment_credential(inp.get("address", "")) == script_key}
        allowlisted = any(_is_allowlisted(a, network) for a in group_addrs)
        if allowlisted:
            w_ex, w_eu, w_ni, w_rc = _reweight_without_extraction()
            s_extraction = 0.0
            s_extraction_lov = 0.0
            s_extraction_assets = 0.0
        else:
            w_ex = float(_W["extraction"])
            w_eu = float(_W["exunits_inv"])
            w_ni = float(_W["inputs"])
            w_rc = float(_W["recurrence"])

        raw = (
            w_ex * s_extraction
            + w_eu * s_exunits_inv
            + w_ni * s_inputs
            + w_rc * s_recurrence
        )
        final = finalise_score(raw)

        # Band floor for confirmed structural double-satisfaction. The gate
        # already required ≥2 inputs from the same script plus a spend
        # redeemer; if on top of that ``s_exunits_inv`` saturates (the
        # validator did near-zero work per input), we have a high-confidence
        # "lazy validator" fingerprint that is unlikely to occur on legitimate
        # txs. The weighted score under §4.4.3 is biased toward value
        # extraction, so a low-value structural exploit can score in the
        # Moderate band even when the structural confirmation is strong.
        # Floor the band to High in that case so operators triage the right
        # signal first. Allowlisted scripts are exempt: legitimate batchers
        # often run minimal per-input CPU by design. Threshold and floor
        # tunable via multiple_sat.lazy_validator_threshold / _floor.
        # Uniform-sweep guard. When the tx fingerprint is "owner sweeping
        # their own script UTxOs" (many inputs, identical spend redeemers,
        # no script return), suppress the lazy-validator floor: the gate
        # has fired and the weighted score still records the structural
        # signals, but we do not artificially elevate the band. Real
        # double-satisfaction exploits have asymmetric satisfaction
        # arguments and write the satisfying value (NFT / payment) to a
        # distinct address shape that this predicate rejects.
        uniform_sweep = _is_uniform_sweep(
            script_key, n_inputs, outputs, spend_redeemer_payloads,
        )

        # Extraction sanity gate: double-satisfaction requires value to
        # leave the script. State-machine contracts that consume 2 of
        # their own UTxOs and write state back have ``s_extraction = 0``
        # and would otherwise have every cheap iteration floored to High.
        # The minimum is well below CTF 05's small-drain extraction signal
        # so genuine low-value exploits still floor.
        floor_applies = (
            not allowlisted
            and not uniform_sweep
            and s_exunits_inv > _LAZY_VALIDATOR_THRESHOLD
            and s_extraction > _LAZY_VALIDATOR_EXTRACTION_MIN
        )
        if floor_applies:
            final = max(final, _LAZY_VALIDATOR_FLOOR)

        # When the sweep guard fires AND the script is also allowlisted,
        # the allowlist reweight redistributes the extraction weight onto
        # s_inputs, which is saturated for the very same sweep (n_inputs
        # large, p99=10). The reweighted score then climbs back above the
        # High threshold, undoing the guard's intent. Cap the final at the
        # top of Moderate so the sweep classification stands regardless of
        # the allowlist path.
        if uniform_sweep:
            final = min(final, BAND_MODERATE_MAX)

        # The baseline source reported is the "most specific tier actually used"
        # across the four features. Prefer per_script > per_policy > global > bootstrap.
        bl_source = _dominant_source([bl_nv, bl_na, bl_ex, bl_ni, bl_rc])

        # Suppress benign multi-input script spends that are not double
        # satisfaction: an owner consolidating their own UTxOs (uniform sweep),
        # or a tx that returns value TO the script (state continuation, not
        # extraction). Gated on ``not floor_applies`` so a high-confidence
        # lazy-validator exploit (already floored to High) is never suppressed,
        # and the CTF-01 marketplace double-sat (uniform=False, value_returned=0,
        # Moderate) is unaffected. These two signals are exactly what the
        # extraction-assets axis cannot distinguish on its own.
        if not floor_applies and (uniform_sweep or lovelace_out_at_script > 0):
            return ScorerResult.no_finding(
                sub_scores={
                    "s_extraction": round(s_extraction, 4),
                    "s_exunits_inv": round(s_exunits_inv, 4),
                    "s_inputs": round(s_inputs, 4),
                    "s_recurrence": round(s_recurrence, 4),
                    "n_inputs_same_script": float(n_inputs),
                    "uniform_sweep": bool(uniform_sweep),
                    "value_returned_lovelace": int(lovelace_out_at_script),
                },
                baseline_source=bl_source,
            )

        reasons = []
        if s_extraction_lov > _REASON_T:
            reasons.append("large_net_value_extraction")
        if s_extraction_assets > _REASON_T:
            reasons.append("native_asset_extraction")
        if s_exunits_inv > _REASON_T:
            reasons.append("low_exunits_per_input")
        if floor_applies:
            reasons.append("lazy_validator_band_floor")
        if s_inputs > _REASON_T:
            reasons.append("high_n_inputs_same_script")
        if allowlisted:
            reasons.append("allowlisted_batch_script")
        if uniform_sweep:
            reasons.append("uniform_script_sweep_guard")

        # ``lovelace_in_at_script`` / ``lovelace_out_at_script`` are already
        # computed at the top of this method via _compute_lovelace_flow; reuse
        # them here instead of re-iterating inputs/outputs.
        redeemer_count = len(spend_redeemer_payloads)

        return ScorerResult(
            score=final,
            sub_scores={
                "s_extraction": round(s_extraction, 4),
                "s_extraction_lov": round(s_extraction_lov, 4),
                "s_extraction_assets": round(s_extraction_assets, 4),
                "s_exunits_inv": round(s_exunits_inv, 4),
                "s_inputs": round(s_inputs, 4),
                "s_recurrence": round(s_recurrence, 4),
                "n_inputs_same_script": float(n_inputs),
                "n_assets_out_of_script": float(n_assets_out),
            },
            reasons=reasons,
            baseline_source=bl_source,
            evidence={
                "n_inputs_same_script": int(n_inputs),
                "redeemer_count": int(redeemer_count),
                "redeemer_input_ratio": round(redeemer_count / max(1, n_inputs), 4),
                "cpu_units_total": int(total_cpu),
                "cpu_units_per_input": int(exunits_per_input),
                "value_extracted_lovelace": int(net_value),
                "value_returned_lovelace": int(lovelace_out_at_script),
                "value_input_lovelace": int(lovelace_in_at_script),
                "n_assets_extracted": int(n_assets_out),
                "target_script_address": representative_addr,
                # ``lovelace_full_drain`` is deliberately narrow: it records
                # only that no lovelace was returned to the script. The
                # canonical NFT-marketplace double-sat exploits drain native
                # assets while the script's lovelace position barely moves;
                # those show ``lovelace_full_drain=False`` but
                # ``n_assets_extracted > 0``. The UI should reference both.
                "lovelace_full_drain": bool(
                    lovelace_out_at_script == 0 and lovelace_in_at_script > 0
                ),
                "allowlisted": bool(allowlisted),
                "uniform_sweep": bool(uniform_sweep),
            },
        )


def _dominant_source(sources: List[str]) -> str:
    """Return the most specific baseline tier used.

    Priority: per_script > per_policy > global > bootstrap. Since
    :func:`resolved_or_bootstrap` guarantees one of these four values,
    the final fallback is "bootstrap" rather than a sentinel for the
    unexpected (which would indicate a programming error upstream).
    """
    order = ["per_script", "per_policy", "global", "bootstrap"]
    for tier in order:
        if tier in sources:
            return tier
    return "bootstrap"
