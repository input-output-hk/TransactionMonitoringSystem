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

from app.analysis.normalise import normalise, normalise_inverted
from app.analysis.scorer_config import (
    get as _get_cfg,
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

            result = self._score_utxo(out, addr, network, policy_count, token_count)
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

        reasons = []
        if s_bytes > _REASON_T:
            reasons.append("high_value_cbor_bytes")
        if s_assets > _REASON_T:
            reasons.append("many_distinct_assets")
        if s_ada > _REASON_T:
            reasons.append("low_lovelace_amount")
        # Composite reason: when all three primary signals saturate at a
        # script-address output, the shape is the canonical value-bloat DoS
        # signature, not retail dust spam routed at a contract. Surfacing
        # this lets the analyst distinguish "bloat the contract so it cannot
        # be used" from "spray dust at random addresses" without renaming
        # the class column.
        #
        # Convenience composite over the three primary reasons; downstream
        # could derive the same predicate, but emitting it here keeps the
        # analyst path uniform.
        #
        # Threshold: each sub-signal must clear ``reason_threshold`` (0.5
        # by default). The composite fires on the *shape*; the score still
        # conveys severity. Matches the existing pattern used for
        # ``lazy_validator_band_floor`` in multiple_sat.
        if (
            feat_mod.is_script_address(address)
            and s_bytes > _REASON_T
            and s_assets > _REASON_T
            and s_ada > _REASON_T
        ):
            reasons.append("script_value_bloat_dos")

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
