"""Fake Token Distribution attack scorer (Class 8).

Detects minting transactions that create tokens impersonating known legitimate
assets and distribute them widely.  The attack exploits the permissionless
nature of Cardano's native asset system: anyone can mint a token with an
arbitrary name under any policy ID.

Two sub-pipelines:
  Identity Deception (0.60): token name similarity, Unicode suspicion, CIP-25 similarity
  Distribution Pattern (0.40): recipient count, mint ratio, policy age, recurrence

Sub-scores (Polimi Section 4.8.3):
  Identity:  tokenname_similarity (0.40), unicode_suspicion (0.35), cip25_similarity (0.25)
  Distribution: recipient_count (0.40), mint_ratio_inv (0.30), policy_age_inv (0.20), recurrence (0.10)
"""

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional

from rapidfuzz import fuzz

from app.analysis.normalise import normalise, normalise_inverted, resolve_baseline
from app.analysis.scorers.base import BaseScorer, ScorerResult
from app.analysis import external

logger = logging.getLogger(__name__)

# Fixed anchors (Polimi Section 5.4)
_NAME_SIM_P50 = 0.80
_NAME_SIM_P99 = 0.97
_UNICODE_P50 = 0.0
_UNICODE_P99 = 0.60
_CIP25_P50 = 0.0
_CIP25_P99 = 0.80
_POLICY_AGE_INV_P50 = 1 / 100_000   # ~55 hours
_POLICY_AGE_INV_P99 = 1 / 5_000     # ~2.7 hours

# Minimum similarity threshold for gate
T_SIM_MIN = 0.80

EPSILON = 1e-6


def _normalize_token_name(name: str) -> str:
    """NFKC normalize and strip zero-width characters."""
    normalized = unicodedata.normalize("NFKC", name)
    # Strip zero-width characters
    normalized = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", normalized)
    return normalized


def _compute_tokenname_similarity(name: str, legit_name: str) -> float:
    """Levenshtein similarity after Unicode normalization."""
    n1 = _normalize_token_name(name).lower()
    n2 = _normalize_token_name(legit_name).lower()
    if not n1 or not n2:
        return 0.0
    return fuzz.ratio(n1, n2) / 100.0


def _compute_unicode_suspicion(name: str) -> float:
    """Score Unicode suspicion: homoglyphs, zero-width chars, mixed scripts."""
    score = 0.0

    # Check for zero-width characters in original name
    zw_chars = set("\u200b\u200c\u200d\ufeff\u00ad")
    if any(c in zw_chars for c in name):
        score += 0.4

    # Check for mixed Unicode scripts
    scripts = set()
    for c in name:
        if c.isalpha():
            try:
                script = unicodedata.name(c, "").split()[0]
                scripts.add(script)
            except (ValueError, IndexError):
                pass
    if len(scripts) > 1:
        score += 0.3

    # Check for common homoglyphs (Cyrillic/Greek lookalikes)
    homoglyphs = set("аеіоруАЕІОРУ" + "αβγδεζηθικλμνξοπρστυφχψω")
    if any(c in homoglyphs for c in name):
        score += 0.3

    return min(1.0, score)


def _compute_cip25_similarity(
    tx_metadata: Optional[Dict], legit_name: str,
) -> float:
    """Score CIP-25 metadata similarity against a known legitimate token."""
    if not tx_metadata or not isinstance(tx_metadata, dict):
        return 0.0

    # CIP-25 metadata is under label 721
    label_721 = tx_metadata.get("721") or tx_metadata.get(721)
    if not label_721 or not isinstance(label_721, dict):
        return 0.0

    # Flatten CIP-25 metadata to find name/ticker/description fields
    text_parts = []
    _flatten_cip25(label_721, text_parts)
    if not text_parts:
        return 0.0

    # Check similarity of extracted fields against legitimate name
    max_sim = 0.0
    legit_lower = legit_name.lower()
    for part in text_parts:
        sim = fuzz.ratio(part.lower(), legit_lower) / 100.0
        max_sim = max(max_sim, sim)

    return max_sim


def _flatten_cip25(obj: Any, parts: List[str], depth: int = 0):
    """Recursively extract string values from CIP-25 metadata."""
    if depth > 5:
        return
    if isinstance(obj, str) and len(obj) > 1:
        parts.append(obj)
    elif isinstance(obj, dict):
        for key, val in obj.items():
            if key in ("name", "ticker", "description", "image"):
                if isinstance(val, str):
                    parts.append(val)
            _flatten_cip25(val, parts, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _flatten_cip25(item, parts, depth + 1)


def _decode_hex_asset_name(hex_name: str) -> str:
    """Decode a hex-encoded asset name to UTF-8, falling back to the raw string."""
    try:
        return bytes.fromhex(hex_name).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return hex_name


def _extract_minted_assets(raw_data: Dict, metadata: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """Extract minted assets from raw_data.mint.

    Resolves human-readable names from: (1) hex decoding of the asset name,
    (2) CIP-25 metadata under label 721.
    """
    mint = raw_data.get("mint")
    if not mint or not isinstance(mint, dict):
        return []

    # Build lookup from CIP-25 metadata: (policy_id, hex_asset_name) -> name
    cip25_names: Dict[tuple, str] = {}
    if metadata and isinstance(metadata, dict):
        label_721 = metadata.get("721") or metadata.get(721)
        if isinstance(label_721, dict):
            for pid, token_map in label_721.items():
                if isinstance(token_map, dict):
                    for asset_key, asset_meta in token_map.items():
                        if isinstance(asset_meta, dict):
                            name = asset_meta.get("name") or asset_meta.get("ticker")
                            if name:
                                cip25_names[(str(pid), str(asset_key))] = str(name)

    assets = []
    for policy_id, token_map in mint.items():
        if not isinstance(token_map, dict):
            continue
        for hex_asset_name, qty in token_map.items():
            qty_int = int(qty) if qty else 0
            if qty_int > 0:  # only mints, not burns
                # Resolve human-readable name: prefer CIP-25, then hex decode
                display_name = (
                    cip25_names.get((policy_id, hex_asset_name))
                    or _decode_hex_asset_name(hex_asset_name)
                )
                assets.append({
                    "policy_id": policy_id,
                    "token_name": display_name,
                    "token_name_hex": hex_asset_name,
                    "quantity": qty_int,
                })
    return assets


class FakeTokenScorer(BaseScorer):
    name = "fake_token"

    def gate(self, features: Dict[str, Any]) -> bool:
        """Minting tx where at least one token name matches a legitimate token."""
        raw_data = features.get("raw_data")
        if not raw_data or not isinstance(raw_data, dict):
            return False

        minted = _extract_minted_assets(raw_data, features.get("metadata"))
        if not minted:
            return False

        legit_tokens = external.get_legitimate_tokens()
        for asset in minted:
            for legit_name, legit_policies in legit_tokens.items():
                sim = _compute_tokenname_similarity(
                    asset["token_name"], legit_name,
                )
                if sim >= T_SIM_MIN and asset["policy_id"] not in legit_policies:
                    return True
        return False

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        raw_data = features.get("raw_data", {})
        network = features.get("network", "")
        metadata = features.get("metadata")

        minted = _extract_minted_assets(raw_data, metadata)
        legit_tokens = external.get_legitimate_tokens()

        # Find best candidate (highest similarity, policy mismatch)
        best_candidate = None
        best_sim = 0.0
        best_legit_name = ""

        for asset in minted:
            for legit_name, legit_policies in legit_tokens.items():
                if asset["policy_id"] in legit_policies:
                    continue
                sim = _compute_tokenname_similarity(
                    asset["token_name"], legit_name,
                )
                if sim >= T_SIM_MIN and sim > best_sim:
                    best_sim = sim
                    best_candidate = asset
                    best_legit_name = legit_name

        if not best_candidate:
            return ScorerResult(score=0.0)

        # ----- Identity Deception sub-pipeline (weight = 0.60) -----

        s_name = normalise(best_sim, p50=_NAME_SIM_P50, p99=_NAME_SIM_P99)

        # Check unicode suspicion on the decoded hex name (preserves zero-width chars)
        hex_decoded = _decode_hex_asset_name(best_candidate.get("token_name_hex", ""))
        unicode_score = _compute_unicode_suspicion(hex_decoded or best_candidate["token_name"])
        s_unicode = normalise(unicode_score, p50=_UNICODE_P50, p99=_UNICODE_P99)

        cip25_sim = _compute_cip25_similarity(metadata, best_legit_name)
        s_cip25 = normalise(cip25_sim, p50=_CIP25_P50, p99=_CIP25_P99)

        identity_score = (
            0.40 * s_name
            + 0.35 * s_unicode
            + 0.25 * s_cip25
        )

        # ----- Distribution Pattern sub-pipeline (weight = 0.40) -----

        policy_id = best_candidate["policy_id"]

        # Count distinct recipient addresses (not total output count)
        outputs = raw_data.get("outputs", [])
        recipient_addrs = set()
        for out in outputs:
            addr = out.get("address", "")
            if addr:
                recipient_addrs.add(addr)
        recipient_count = len(recipient_addrs)

        # recipient_count: per-policy baseline
        p50_rc, p99_rc, bl1 = resolve_baseline(
            "recipient_count", "per_policy", policy_id,
        )
        if bl1 == "missing":
            p50_rc, p99_rc = 1.0, 100.0  # bootstrap
        s_recipients = normalise(recipient_count, p50=p50_rc, p99=p99_rc)

        # mint_to_recipient_ratio inverted
        mint_ratio = best_candidate["quantity"] / (recipient_count + EPSILON)
        p50_mr, p99_mr, bl2 = resolve_baseline(
            "mint_to_recipient_ratio", "per_policy", policy_id,
        )
        if bl2 == "missing":
            p50_mr, p99_mr = 100.0, 100_000.0  # bootstrap
        s_ratio = normalise_inverted(mint_ratio, p50=p50_mr, p99=p99_mr)

        # policy_age inverted: newer policies are more suspicious
        # Use current slot minus first-seen slot for policy; if unavailable,
        # estimate from tx slot (minting tx = policy creation is common)
        current_slot = features.get("slot") or 0
        # For a minting tx, the policy may be new (this tx creates it) or
        # pre-existing. Without a policy registry tracking first-seen slot,
        # assume the policy is new (age ~= 1 slot) which is the suspicious
        # case. If the policy existed before, this overestimates suspicion
        # but that's the safer direction for detection.
        # Policy age defaults to 1 slot (new). On-chain policy age lookup
        # requires indexing transactions by policy ID (deferred to mainnet).
        policy_age_slots = 1
        age_inv = 1.0 / policy_age_slots
        s_policy_age = normalise(
            age_inv, p50=_POLICY_AGE_INV_P50, p99=_POLICY_AGE_INV_P99,
        )

        # Sender recurrence: requires entity clustering (deferred to mainnet)
        s_recurrence = 0.0

        distribution_score = (
            0.40 * s_recipients
            + 0.30 * s_ratio
            + 0.20 * s_policy_age
            + 0.10 * s_recurrence
        )

        # ----- Final combined score -----
        raw = 0.60 * identity_score + 0.40 * distribution_score
        final = round(max(0.0, min(1.0, raw)) * 100, 2)

        bl_source = bl1 if bl1 != "missing" else "bootstrap"

        reasons = []
        if s_name > 0.3:
            reasons.append(f"similar_to_{best_legit_name}")
        if s_unicode > 0.3:
            reasons.append("unicode_suspicion")
        if s_recipients > 0.5:
            reasons.append("mass_distribution")

        return ScorerResult(
            score=final,
            sub_scores={
                "tokenname_similarity": round(s_name, 4),
                "unicode_suspicion": round(s_unicode, 4),
                "cip25_similarity": round(s_cip25, 4),
                "identity_composite": round(identity_score, 4),
                "recipients": round(s_recipients, 4),
                "mint_ratio_inverted": round(s_ratio, 4),
                "policy_age_inverted": round(s_policy_age, 4),
                "sender_recurrence": round(s_recurrence, 4),
                "distribution_composite": round(distribution_score, 4),
                "matched_token": best_legit_name,
                "matched_similarity": round(best_sim, 4),
            },
            reasons=reasons,
            baseline_source=bl_source,
        )
