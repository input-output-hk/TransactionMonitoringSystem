"""Phishing attack scorer (Class 9).

Detects malicious URLs, social engineering content, and deceptive instructions
embedded in on-chain transaction metadata (CIP-0020 label 674, CIP-0025 label
721).  Scores via two sub-pipelines:

  Content   (weight 0.65): URL blacklist match, domain suspicion, social
                           engineering keyword patterns.
  Delivery  (weight 0.35): mass distribution indicators (output count,
                           recipient uniformity, URL recurrence).

Gate condition: metadata must be present with at least one relevant label
containing at least one URL.
"""

import re
import logging
from typing import Any, Dict, List

from rapidfuzz import fuzz

from app.analysis.normalise import normalise, resolve_baseline
from app.analysis.scorer_config import get as _get_cfg, anchor as _anchor
from app.analysis.scorers.base import BaseScorer, ScorerResult
from app.analysis import external

logger = logging.getLogger(__name__)

_CFG = _get_cfg("phishing")
_W_CONTENT = _CFG["weights"]["content"]
_W_DELIVERY = _CFG["weights"]["delivery"]
_W_OVERALL = _CFG["weights"]["overall"]
_FIXED = _CFG["fixed_anchors"]
_BOOT = _CFG["bootstrap_anchors"]
_SIM_RANGE = _CFG["similarity_suspicious_range"]
_SE = _CFG["social_engineering"]
_REASON_T = _CFG["reason_thresholds"]
_CRITICAL_T = float(_CFG["critical_threshold"])
_RELEVANT_LABELS = set(str(x) for x in _CFG["metadata_labels"])

# URL extraction regex — matches http(s) URLs in metadata strings
_URL_RE = re.compile(
    r'https?://[^\s"\'<>\]\)}{,]+',
    re.IGNORECASE,
)


class PhishingScorer(BaseScorer):
    name = "phishing"

    def gate(self, features: Dict[str, Any]) -> bool:
        """Metadata must contain at least one relevant label with a URL.

        Sender allowlist: if any input address matches a known legitimate
        sender, skip scoring to reduce false positives (Polimi Section 4.9.4).
        """
        # Allowlist check
        addresses = features.get("addresses") or []
        if addresses and external.is_sender_allowlisted(addresses):
            return False

        metadata = features.get("metadata")
        if not metadata or not isinstance(metadata, dict):
            return False
        has_relevant = any(str(k) in _RELEVANT_LABELS for k in metadata.keys())
        if not has_relevant:
            return False

        # Check for at least one URL
        urls = self._extract_urls(metadata)
        return len(urls) > 0

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        metadata = features.get("metadata") or {}
        urls = self._extract_urls(metadata)
        if not urls:
            return ScorerResult(score=0.0)

        # ----- Content sub-pipeline (weight = 0.65) -----

        # Sub-score 1a: URL blacklist match (weight 0.40 of content)
        s_blacklist = self._score_blacklist(urls)

        # Sub-score 1b: domain suspicion composite (weight 0.35 of content)
        s_domain = self._score_domain_suspicion(urls)

        # Sub-score 1c: social engineering score (weight 0.25 of content)
        s_social = self._score_social_engineering(metadata)

        content_score = (
            float(_W_CONTENT["blacklist"]) * s_blacklist
            + float(_W_CONTENT["domain"]) * s_domain
            + float(_W_CONTENT["social"]) * s_social
        )

        # ----- Delivery sub-pipeline (weight = 0.35) -----

        # Sub-score 2a: recipient_count (weight 0.35 of delivery)
        # Uses dynamic baselines with bootstrap fallback until Phase 2
        output_count = features.get("output_count", 0)
        p50, p99, bl_source = resolve_baseline("recipient_count")
        if bl_source == "missing":
            p50, p99 = _anchor(_BOOT, "recipient_count")
            bl_source = "bootstrap"
        s_recipients = normalise(output_count, p50=p50, p99=p99)

        # Sub-score 2b: url_hash_recurrence (weight 0.25 of delivery)
        # Requires cross-tx URL indexing (deferred to mainnet)
        s_url_recur = 0.0

        # Sub-score 2c: targeting_score (weight 0.25 of delivery)
        # Requires recipient-to-protocol interaction graph (deferred to mainnet)
        s_targeting = 0.0

        # Sub-score 2d: sender_recurrence (weight 0.15 of delivery)
        # Requires entity clustering (deferred to mainnet)
        s_recurrence = 0.0

        # Spec weights: recipients 0.35, url_recur 0.25, targeting 0.25,
        # recurrence 0.15. Sub-scores 2b-2d are deferred (0.0).
        delivery_score = (
            float(_W_DELIVERY["recipients"]) * s_recipients
            + float(_W_DELIVERY["url_recur"]) * s_url_recur
            + float(_W_DELIVERY["targeting"]) * s_targeting
            + float(_W_DELIVERY["recurrence"]) * s_recurrence
        )

        # ----- Final combined score -----
        raw = (
            float(_W_OVERALL["content"]) * content_score
            + float(_W_OVERALL["delivery"]) * delivery_score
        )
        final_score = round(max(0.0, min(1.0, raw)) * 100, 2)

        sub_scores = {
            "blacklist": round(s_blacklist, 4),
            "domain_suspicion": round(s_domain, 4),
            "social_engineering": round(s_social, 4),
            "content_composite": round(content_score, 4),
            "recipients": round(s_recipients, 4),
            "url_recurrence": round(s_url_recur, 4),
            "targeting": round(s_targeting, 4),
            "sender_recurrence": round(s_recurrence, 4),
            "delivery_composite": round(delivery_score, 4),
        }

        reasons = []
        if s_blacklist > float(_REASON_T["blacklist"]):
            reasons.append("url_blacklist_match")
        if s_domain > float(_REASON_T["domain"]):
            reasons.append("suspicious_domain")
        if s_social > float(_REASON_T["social"]):
            reasons.append("social_engineering_language")
        if s_recipients > float(_REASON_T["recipients"]):
            reasons.append("mass_distribution")

        # Severity classification (Polimi Section 4.9.3)
        severity = None
        if s_blacklist == 1.0:
            severity = "KNOWN_BAD"
        elif content_score >= _CRITICAL_T:
            severity = "SUSPICIOUS_NEW_DOMAIN"
        else:
            severity = "SOCIAL_ENGINEERING"

        return ScorerResult(
            score=final_score,
            sub_scores=sub_scores,
            reasons=reasons,
            baseline_source=bl_source,
            severity=severity,
        )

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _extract_urls(self, metadata: Dict[str, Any]) -> List[str]:
        """Extract all URLs from relevant metadata labels."""
        urls = []
        for label_key in _RELEVANT_LABELS:
            content = metadata.get(label_key) or metadata.get(int(label_key))
            if content is None:
                continue
            text = self._flatten_to_text(content)
            urls.extend(_URL_RE.findall(text))
        return list(set(urls))

    def _flatten_to_text(self, obj: Any) -> str:
        """Recursively flatten a metadata value to a single string."""
        if isinstance(obj, str):
            return obj
        if isinstance(obj, list):
            return " ".join(self._flatten_to_text(item) for item in obj)
        if isinstance(obj, dict):
            parts = []
            for v in obj.values():
                parts.append(self._flatten_to_text(v))
            return " ".join(parts)
        return str(obj) if obj is not None else ""

    def _score_blacklist(self, urls: List[str]) -> float:
        """Score URLs against phishing domain patterns."""
        patterns = external.get_phishing_patterns()
        max_score = 0.0
        for url in urls:
            for pat in patterns:
                if pat.search(url):
                    max_score = max(max_score, 1.0)
        return max_score

    def _score_domain_suspicion(self, urls: List[str]) -> float:
        """Composite domain suspicion: brand similarity to known protocols.

        Domain age scoring requires WHOIS lookup (deferred to mainnet).
        """
        known_domains = external.get_protocol_domains()
        max_brand_sim = 0.0

        for url in urls:
            domain = self._extract_domain(url)
            if not domain:
                continue
            # Strip TLD for comparison
            domain_base = domain.split(".")[0] if "." in domain else domain

            for legit in known_domains:
                legit_base = legit.split(".")[0] if "." in legit else legit
                sim = fuzz.ratio(domain_base.lower(), legit_base.lower()) / 100.0
                # Only count as suspicious if it's similar but NOT an exact match
                if float(_SIM_RANGE["lo"]) < sim < float(_SIM_RANGE["hi"]):
                    max_brand_sim = max(max_brand_sim, sim)

        p50_b, p99_b = _anchor(_FIXED, "brand_sim")
        s_brand = normalise(max_brand_sim, p50=p50_b, p99=p99_b)

        # Domain age: requires WHOIS API (deferred to mainnet).
        # When domain age is available, composite = 0.50 * s_age + 0.50 * s_brand.
        # Until then, use brand similarity as the sole domain suspicion signal.
        return s_brand

    def _score_social_engineering(self, metadata: Dict[str, Any]) -> float:
        """Score metadata text for social engineering patterns."""
        # Flatten all metadata to searchable text
        text = self._flatten_to_text(metadata).lower()
        if not text:
            return 0.0

        # Tier 1: credential request patterns (near-deterministic)
        for pattern in external.TIER1_CREDENTIAL_PATTERNS:
            if pattern.lower() in text:
                return 1.0

        score = 0.0

        # Tier 2: urgency language
        urgency_matches = 0
        for pattern in external.TIER2_URGENCY_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                urgency_matches += 1
        if urgency_matches > 0:
            score += min(
                float(_SE["urgency_cap"]),
                urgency_matches * float(_SE["urgency_increment"]),
            )

        # Tier 3: brand impersonation in suspicious context
        brand_matches = 0
        for brand in external.TIER3_BRAND_NAMES:
            if brand.lower() in text:
                brand_matches += 1
        if brand_matches > 0:
            score += min(
                float(_SE["brand_cap"]),
                brand_matches * float(_SE["brand_increment"]),
            )

        p50_s, p99_s = _anchor(_FIXED, "social_score")
        return normalise(score, p50=p50_s, p99=p99_s)

    def _extract_domain(self, url: str) -> str:
        """Extract the domain from a URL string."""
        try:
            # Strip protocol
            domain = url.split("://", 1)[-1]
            # Strip path
            domain = domain.split("/", 1)[0]
            # Strip port
            domain = domain.split(":", 1)[0]
            return domain.lower()
        except Exception:
            return ""
