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
from app.analysis.scorers.base import BaseScorer, ScorerResult
from app.analysis import external

logger = logging.getLogger(__name__)

# URL extraction regex — matches http(s) URLs in metadata strings
_URL_RE = re.compile(
    r'https?://[^\s"\'<>\]\)}{,]+',
    re.IGNORECASE,
)

# Relevant CIP metadata labels
_RELEVANT_LABELS = {"674", "721"}

# Fixed normalisation anchors (from Polimi spec Section 5.4)
_DOMAIN_AGE_P50 = 1 / 365   # reciprocal: 1-year-old domain is baseline
_DOMAIN_AGE_P99 = 1 / 7     # 7-day-old domain is highly suspicious
_BRAND_SIM_P50 = 0.0
_BRAND_SIM_P99 = 0.85
_SE_SCORE_P50 = 0.0
_SE_SCORE_P99 = 0.60
_TARGETING_P50 = 0.05
_TARGETING_P99 = 0.50


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
            0.40 * s_blacklist
            + 0.35 * s_domain
            + 0.25 * s_social
        )

        # ----- Delivery sub-pipeline (weight = 0.35) -----

        # Sub-score 2a: recipient_count (weight 0.35 of delivery)
        # Uses dynamic baselines with bootstrap fallback until Phase 2
        output_count = features.get("output_count", 0)
        p50, p99, bl_source = resolve_baseline("recipient_count")
        if bl_source == "missing":
            # Bootstrap anchors until baseline infra is live (Phase 2)
            p50, p99 = 1.0, 50.0
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
            0.35 * s_recipients
            + 0.25 * s_url_recur
            + 0.25 * s_targeting
            + 0.15 * s_recurrence
        )

        # ----- Final combined score -----
        raw = 0.65 * content_score + 0.35 * delivery_score
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
        if s_blacklist > 0.5:
            reasons.append("url_blacklist_match")
        if s_domain > 0.5:
            reasons.append("suspicious_domain")
        if s_social > 0.3:
            reasons.append("social_engineering_language")
        if s_recipients > 0.5:
            reasons.append("mass_distribution")

        # Severity classification (Polimi Section 4.9.3)
        severity = None
        if s_blacklist == 1.0:
            severity = "KNOWN_BAD"
        elif content_score >= 0.60:
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
                if 0.5 < sim < 1.0:
                    max_brand_sim = max(max_brand_sim, sim)

        s_brand = normalise(max_brand_sim, p50=_BRAND_SIM_P50, p99=_BRAND_SIM_P99)

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
            score += min(0.6, urgency_matches * 0.15)

        # Tier 3: brand impersonation in suspicious context
        brand_matches = 0
        for brand in external.TIER3_BRAND_NAMES:
            if brand.lower() in text:
                brand_matches += 1
        if brand_matches > 0:
            score += min(0.3, brand_matches * 0.10)

        return normalise(score, p50=_SE_SCORE_P50, p99=_SE_SCORE_P99)

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
