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

from app.analysis.normalise import (
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

logger = logging.getLogger(__name__)

_CFG = _get_cfg("token_dust")
_W = _CFG["weights"]
_BOOT = _CFG["bootstrap_anchors"]
_REASON_T = float(_CFG["reason_threshold"])
_MIN_TOKEN_COUNT = int(_CFG["gate"]["min_token_count"])
_DOS_ASSET_MIN = int(_CFG["dos_asset_min"])


def _max_assets_per_policy(value: Dict[str, Any]) -> int:
    """Return the largest count of distinct asset names under any single policy.

    Reported in ``sub_scores`` for observability: lets an analyst see
    whether a flagged bundle is one-policy-many-names (CTF 06: 80/1),
    many-policies-few-names (symmetric DoS shape), or evenly distributed.
    Not used as a gating threshold (total pair count handles both shapes
    symmetrically; see :data:`_DOS_ASSET_MIN`).

    Ignores the lovelace entry under both Ogmios v5 (``"lovelace"``) and
    v6 (``"ada"``) shapes.
    """
    if not isinstance(value, dict):
        return 0
    best = 0
    for policy, inner in value.items():
        if policy in ("ada", "lovelace"):
            continue
        if isinstance(inner, dict):
            best = max(best, len(inner))
    return best


_ALLOWLIST_PREFIXES: Dict[str, frozenset] = _load_network_map(
    _CFG.get("allowlist_prefixes"),
    scorer="token_dust",
    field="allowlist_prefixes",
    collect=frozenset,
)
_ALLOWLIST_POLICIES: Dict[str, frozenset] = _load_network_map(
    _CFG.get("allowlist_policies"),
    scorer="token_dust",
    field="allowlist_policies",
    collect=frozenset,
)


def _is_allowlisted_utxo(address: str, value: Dict[str, Any], network: str) -> bool:
    """A UTxO is allowlisted when its address matches a network-scoped
    prefix OR every non-ADA policy in its value is under a network-scoped
    allowlisted policy.

    The policy-set check requires *all* policies to be allowlisted: a
    single non-allowlisted policy means the bundle could still carry
    attacker-controlled dust, so the scorer should run.

    Network scoping prevents a preprod entry (where anyone can register a
    policy) from suppressing alerts on mainnet under the same hash.
    """
    prefixes = _ALLOWLIST_PREFIXES.get(network, frozenset())
    if address and any(address.startswith(p) for p in prefixes):
        return True
    policies_set = _ALLOWLIST_POLICIES.get(network, frozenset())
    if not policies_set:
        return False
    policies = [p for p in value.keys() if p not in ("ada", "lovelace")]
    return bool(policies) and all(p in policies_set for p in policies)


class TokenDustScorer(BaseScorer):
    name = "token_dust"

    def gate(self, features: Dict[str, Any]) -> bool:
        """At least one script output must carry >= min_token_count live assets.

        A single-NFT UTxO cannot bloat the Value field's CBOR enough to be a
        dust-of-many-tokens attack, so we require a small bundle to enter
        scoring. Threshold lives in ``detection.yaml`` (``gate.min_token_count``).
        """
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
            if token_count >= _MIN_TOKEN_COUNT:
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
            policy_count, token_count = feat_mod.count_assets(value)
            if token_count < _MIN_TOKEN_COUNT:
                continue
            if _is_allowlisted_utxo(addr, value, network):
                # Known multi-asset protocol UTxO (e.g. lending offer carrying
                # offer-NFT + reference-NFT + lent-token batch at min-ADA).
                # Structurally indistinguishable from a value-bloat bundle, so
                # the scorer must be suppressed by config rather than logic.
                continue

            result = self._score_utxo(out, addr, network, policy_count, token_count)
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
        self,
        output: Dict,
        address: str,
        network: str,
        policy_count: int,
        token_count: int,
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

        # Resolve baselines (per-script -> global -> bootstrap)
        p50_cb, p99_cb, bl1 = _resolve(
            "value_cbor_bytes", "per_script", address, network,
            _BOOT, "value_cbor_bytes",
        )
        p50_ac, p99_ac, _ = _resolve(
            "unique_token_count", "per_script", address, network,
            _BOOT, "unique_token_count",
        )
        p50_ada, p99_ada, _ = _resolve(
            "ada_amount", "per_script", address, network,
            _BOOT, "ada_amount",
        )
        bl_source = bl1

        # Sub-scores
        s_bytes = normalise(value_cbor, p50=p50_cb, p99=p99_cb)
        s_assets = normalise(token_count, p50=p50_ac, p99=p99_ac)
        s_ada = normalise_inverted(ada_amount, p50=p50_ada, p99=p99_ada)
        # Sender recurrence: requires entity clustering (deferred to mainnet)
        s_recurrence = 0.0

        raw = (
            float(_W["bytes"]) * s_bytes
            + float(_W["assets"]) * s_assets
            + float(_W["ada_inv"]) * s_ada
            + float(_W["recurrence"]) * s_recurrence
        )
        final = finalise_score(raw)

        # Structural discriminator: total distinct (policy, name) pairs
        # in the bundle. Real value-bloat DoS shapes carry many pairs
        # (CTF 06: 80); protocol multi-asset UTxOs sit at <=6 across the
        # cluster observed on 2026-05-15. Gates the composite reason and
        # the Moderate cap below. Symmetric to "many names under one
        # policy" vs "many policies x few names" because both shapes
        # add equivalent CBOR overhead; an observability-only
        # ``max_assets_per_policy`` is also recorded for analyst review.
        max_per_policy = _max_assets_per_policy(value)

        reasons = []
        if s_bytes > _REASON_T:
            reasons.append("high_value_cbor_bytes")
        if s_assets > _REASON_T:
            reasons.append("many_distinct_assets")
        if s_ada > _REASON_T:
            reasons.append("low_lovelace_amount")
        # Composite reason: when all three primary signals saturate at a
        # script-address output AND the bundle concentrates many names
        # under one policy, the shape is the canonical value-bloat DoS
        # signature: an attacker minted many distinct names under a one-
        # shot policy and forced the contract to carry them forward.
        # Protocol multi-asset UTxOs (lending offers, DEX pool state)
        # also saturate the three primary axes but distribute their
        # names across multiple policies; gating on max_assets_per_policy
        # discriminates the two without per-protocol allowlists.
        if (
            feat_mod.is_script_address(address)
            and s_bytes > _REASON_T
            and s_assets > _REASON_T
            and s_ada > _REASON_T
            and token_count >= _DOS_ASSET_MIN
        ):
            reasons.append("script_value_bloat_dos")

        # Band cap for low-asset bundles. The threat model is "many
        # distinct (policy, name) pairs bloating CBOR every time the
        # contract is used"; a 4-pair bundle adds ~200 bytes of CBOR
        # overhead, far below any meaningful fraction of the 16KB tx
        # limit. The three saturated sub-scores still record severity
        # in their own right (e.g. anomalously low ADA for the global
        # baseline), but the band must reflect the structural fact that
        # this is not an exploitable shape. Symmetric with multiple_sat's
        # uniform_sweep_guard cap.
        if token_count < _DOS_ASSET_MIN:
            final = min(final, BAND_MODERATE_MAX)

        return ScorerResult(
            score=final,
            sub_scores={
                "value_cbor_bytes": round(s_bytes, 4),
                "unique_assetclass_count": round(s_assets, 4),
                "lovelace_inverted": round(s_ada, 4),
                "sender_recurrence": round(s_recurrence, 4),
                "max_assets_per_policy": float(max_per_policy),
            },
            reasons=reasons,
            baseline_source=bl_source,
            evidence={
                "unique_asset_count": int(token_count),
                "policy_count": int(policy_count),
                "value_cbor_bytes_raw": int(value_cbor),
                "max_assets_per_policy": int(max_per_policy),
                "lovelace_amount": int(ada_amount),
                "target_script_address": address,
            },
        )
