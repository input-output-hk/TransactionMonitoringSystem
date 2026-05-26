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

from app.analysis.normalise import normalise, normalise_inverted
from app.analysis.scorer_config import (
    get as _get_cfg,
    anchor as _anchor,
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import BaseScorer, ScorerResult, finalise_score
from app.analysis import external

logger = logging.getLogger(__name__)

_CFG = _get_cfg("fake_token")
_W_IDENT = _CFG["weights"]["identity"]
_W_DIST = _CFG["weights"]["distribution"]
_W_OVERALL = _CFG["weights"]["overall"]
_FIXED = _CFG["fixed_anchors"]
_BOOT = _CFG["bootstrap_anchors"]
_UNI_SCORES = _CFG["unicode_scores"]
_REASON_T = _CFG["reason_thresholds"]
T_SIM_MIN = float(_CFG["similarity_threshold"])

EPSILON = 1e-6


# Visual-confusables table. NFKC does not fold cross-script visual
# lookalikes (Greek capital Nu vs Latin N, Cyrillic Straight U vs Latin
# Y, etc.) because those are distinct characters under Unicode
# semantics, not compatibility equivalents. UTR #39 covers them via a
# separate confusables specification. This is a curated subset focused
# on capitals commonly used in token tickers, broad enough to catch the
# observed forge attacks without pulling the full confusables.txt
# table. Extend when a new homoglyph slips through the gate.
_CONFUSABLES = str.maketrans({
    # Greek capitals visually identical to Latin
    "\u0391": "A",  # \u0391 GREEK CAPITAL LETTER ALPHA
    "\u0392": "B",  # \u0392 BETA
    "\u0395": "E",  # \u0395 EPSILON
    "\u0396": "Z",  # \u0396 ZETA
    "\u0397": "H",  # \u0397 ETA
    "\u0399": "I",  # \u0399 IOTA
    "\u039a": "K",  # \u039a KAPPA
    "\u039c": "M",  # \u039c MU
    "\u039d": "N",  # \u039d NU
    "\u039f": "O",  # \u039f OMICRON
    "\u03a1": "P",  # \u03a1 RHO
    "\u03a4": "T",  # \u03a4 TAU
    "\u03a5": "Y",  # \u03a5 UPSILON
    "\u03a7": "X",  # \u03a7 CHI
    # Greek lowercase visually similar to Latin
    "\u03b1": "a",  # \u03b1 ALPHA
    "\u03b5": "e",  # \u03b5 EPSILON
    "\u03b9": "i",  # \u03b9 IOTA
    "\u03bd": "v",  # \u03bd NU (looks like Latin v)
    "\u03bf": "o",  # \u03bf OMICRON
    "\u03c1": "p",  # \u03c1 RHO
    "\u03c5": "u",  # \u03c5 UPSILON
    # Cyrillic capitals visually identical to Latin
    "\u0410": "A",  # \u0410
    "\u0412": "B",  # \u0412
    "\u0415": "E",  # \u0415
    "\u041a": "K",  # \u041a
    "\u041c": "M",  # \u041c
    "\u041d": "H",  # \u041d (Cyrillic En, looks like Latin H)
    "\u041e": "O",  # \u041e
    "\u0420": "P",  # \u0420
    "\u0421": "C",  # \u0421
    "\u0422": "T",  # \u0422
    "\u0423": "Y",  # \u0423
    "\u0425": "X",  # \u0425
    "\u0406": "I",  # \u0406
    "\u04ae": "Y",  # \u04ae CYRILLIC CAPITAL LETTER STRAIGHT U
    "\u0408": "J",  # \u0408
    # Cyrillic lowercase visually similar to Latin
    "\u0430": "a",  # \u0430
    "\u0435": "e",  # \u0435
    "\u043e": "o",  # \u043e
    "\u0440": "p",  # \u0440
    "\u0441": "c",  # \u0441
    "\u0443": "y",  # \u0443
    "\u0445": "x",  # \u0445
    "\u0456": "i",  # \u0456
})


def _normalize_token_name(name: str) -> str:
    """NFKC normalize, strip zero-width characters, and fold visual
    confusables to Latin equivalents.

    The confusables fold runs AFTER NFKC because NFKC may decompose
    some characters into base + combining marks; folding base-character
    confusables on the decomposed form catches more cases.
    """
    normalized = unicodedata.normalize("NFKC", name)
    normalized = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", normalized)
    return normalized.translate(_CONFUSABLES)


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
        score += float(_UNI_SCORES["zero_width"])

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
        score += float(_UNI_SCORES["mixed_scripts"])

    # Homoglyph detection. Single source of truth is the keys of
    # _CONFUSABLES (the same table used by _normalize_token_name to fold
    # confusables before similarity comparison), so the gate and this
    # sub-score cannot drift. Any non-Latin character that has a Latin
    # confusable in the table contributes the configured homoglyph
    # score; the score caps at 1.0 via the min() return.
    if any(ord(c) in _CONFUSABLES for c in name):
        score += float(_UNI_SCORES["homoglyphs"])

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

        legit_tokens = external.get_legitimate_tokens(features.get("network", ""))
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
        legit_tokens = external.get_legitimate_tokens(network)

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

        # ----- Identity Deception sub-pipeline -----

        p50_n, p99_n = _anchor(_FIXED, "name_sim")
        s_name = normalise(best_sim, p50=p50_n, p99=p99_n)

        # Check unicode suspicion on the decoded hex name (preserves zero-width chars)
        hex_decoded = _decode_hex_asset_name(best_candidate.get("token_name_hex", ""))
        unicode_score = _compute_unicode_suspicion(hex_decoded or best_candidate["token_name"])
        p50_u, p99_u = _anchor(_FIXED, "unicode")
        s_unicode = normalise(unicode_score, p50=p50_u, p99=p99_u)

        cip25_sim = _compute_cip25_similarity(metadata, best_legit_name)
        p50_c, p99_c = _anchor(_FIXED, "cip25")
        s_cip25 = normalise(cip25_sim, p50=p50_c, p99=p99_c)

        identity_score = (
            float(_W_IDENT["name"]) * s_name
            + float(_W_IDENT["unicode"]) * s_unicode
            + float(_W_IDENT["cip25"]) * s_cip25
        )

        # ----- Distribution Pattern sub-pipeline -----

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
        p50_rc, p99_rc, bl1 = _resolve(
            "recipient_count", "per_policy", policy_id, network,
            _BOOT, "recipient_count",
        )
        s_recipients = normalise(recipient_count, p50=p50_rc, p99=p99_rc)

        # mint_to_recipient_ratio inverted
        mint_ratio = best_candidate["quantity"] / (recipient_count + EPSILON)
        p50_mr, p99_mr, _ = _resolve(
            "mint_to_recipient_ratio", "per_policy", policy_id, network,
            _BOOT, "mint_to_recipient_ratio",
        )
        s_ratio = normalise_inverted(mint_ratio, p50=p50_mr, p99=p99_mr)

        # policy_age inverted: newer policies are more suspicious.
        # Without a policy registry, assume age = 1 slot (most suspicious case).
        # Safer direction for detection; on-chain lookup is a future enhancement.
        policy_age_slots = 1
        age_inv = 1.0 / policy_age_slots
        p50_pa, p99_pa = _anchor(_FIXED, "policy_age_inv")
        s_policy_age = normalise(age_inv, p50=p50_pa, p99=p99_pa)

        # Sender recurrence: requires entity clustering (deferred to mainnet)
        s_recurrence = 0.0

        distribution_score = (
            float(_W_DIST["recipients"]) * s_recipients
            + float(_W_DIST["ratio"]) * s_ratio
            + float(_W_DIST["policy_age"]) * s_policy_age
            + float(_W_DIST["recurrence"]) * s_recurrence
        )

        # ----- Final combined score -----
        raw = (
            float(_W_OVERALL["identity"]) * identity_score
            + float(_W_OVERALL["distribution"]) * distribution_score
        )
        final = finalise_score(raw)

        bl_source = bl1

        reasons = []
        if s_name > float(_REASON_T["name"]):
            reasons.append(f"similar_to_{best_legit_name}")
        if s_unicode > float(_REASON_T["unicode"]):
            reasons.append("unicode_suspicion")
        if s_recipients > float(_REASON_T["recipients"]):
            reasons.append("mass_distribution")

        scan_name = hex_decoded or best_candidate["token_name"]
        confusables: List[Dict[str, str]] = []
        for c in dict.fromkeys(scan_name):
            mapped = _CONFUSABLES.get(ord(c))
            if mapped is None:
                continue
            target = chr(mapped) if isinstance(mapped, int) else str(mapped)
            confusables.append({"from_char": c, "to_char": target})

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
            evidence={
                "matched_token": best_legit_name,
                "matched_similarity": round(best_sim, 4),
                "fake_policy_id": best_candidate.get("policy_id", ""),
                "fake_asset_name_hex": best_candidate.get("token_name_hex", ""),
                "fake_asset_name_ascii": best_candidate.get("token_name", ""),
                "fake_quantity": int(best_candidate.get("quantity", 0)),
                "legit_policy_ids": list(legit_tokens.get(best_legit_name, [])),
                "recipient_count": int(recipient_count),
                "cip25_similarity_raw": round(cip25_sim, 4),
                "unicode_confusables": confusables,
            },
        )
