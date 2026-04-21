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
from typing import Any, Dict, List, Optional

import tldextract
from rapidfuzz import fuzz

from app.analysis.normalise import normalise
from app.analysis.scorer_config import (
    get as _get_cfg,
    anchor as _anchor,
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import BaseScorer, ScorerResult, finalise_score
from app.analysis import external

# No-network tldextract: use the PSL snapshot bundled with the wheel rather
# than hitting the network on first use. Safer for offline / sandboxed envs.
_tld = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)


def _registrable_domain(url_or_domain: str) -> Optional[str]:
    """Return the registrable domain (brand + public suffix), e.g.
    'api.andamio.io' -> 'andamio.io', 'foo.co.uk' -> 'foo.co.uk'.
    Returns None for IP addresses or unparseable input."""
    ext = _tld(url_or_domain)
    if not ext.domain or not ext.suffix:
        return None  # IP address, localhost, or non-domain
    return f"{ext.domain}.{ext.suffix}"


def _brand(url_or_domain: str) -> Optional[str]:
    """Return the brand (registrable domain minus public suffix)."""
    ext = _tld(url_or_domain)
    return ext.domain or None

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

# URL extraction regexes.
#
# _URL_RE: strict http(s) URL form. Always preferred when a scheme is present.
# _BARE_DOMAIN_RE: 2+ dot-separated DNS labels with an optional path. Used to
#   catch scheme-less phishing payloads like ``cardano-drop.io/claim`` that
#   CIP-20 messages routinely carry. Matches get validated against
#   tldextract's PSL snapshot (see ``_looks_like_domain``) so bare-word
#   constructs like ``3.14`` or ``version.py`` don't produce false positives.
_URL_RE = re.compile(
    r'https?://[^\s"\'<>\]\)}{,]+',
    re.IGNORECASE,
)
_BARE_DOMAIN_RE = re.compile(
    r'\b(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+[a-z]{2,24}(?:/[^\s"\'<>\]\)}{,]*)?',
    re.IGNORECASE,
)

# Minimum candidate length to consider, post-TLD-validation. Rules out very
# short matches where tldextract's PSL recognises a 2-letter TLD (e.g. ``a.io``
# is technically parseable but almost always noise).
_BARE_DOMAIN_MIN_LEN = 6

# RFC 2606 reserved TLDs. Not in Mozilla's Public Suffix List (and so rejected
# by tldextract) but frequently appear in phishing-harness output and the
# occasional real on-chain simulation. Accepting them closes a detection gap
# without meaningfully expanding the FP surface — these TLDs don't resolve,
# so any on-chain URL using them is almost certainly test / simulation /
# deliberate fake.
_RFC2606_RESERVED_TLDS = frozenset({"test", "example", "invalid", "localhost"})

# TLDs disproportionately used by phishing campaigns. Cheap / free / bulk
# registration plus loose abuse enforcement drives the asymmetry. Cardano's
# legitimate protocol domains concentrate in .org / .io / .com / .finance /
# .net / .store — a URL in an on-chain phishing payload landing on one of
# these TLDs is extra evidence of intent. Bonus only applies when there's
# also Tier-2 phishing text in the same tx, to avoid flagging every legit
# .xyz ENS-adjacent project.
#
# RFC 2606 reserved TLDs (.test, .example, .invalid, .localhost) are also
# included here: no legitimate service can live there, so any on-chain
# URL pointing at one is either a simulation, a test fixture, or an
# attacker-placeholder — all worth boosting score on.
_PHISHING_PRONE_TLDS = frozenset({
    # Cheap / bulk / free registration
    "xyz", "top", "click", "link", "live", "online", "site",
    "space", "loan", "download", "stream", "tk", "ml", "ga", "cf",
    "gdn", "work", "party", "trade", "date", "science",
    # RFC 2606 reserved — non-routable, placeholder use only
    "test", "example", "invalid", "localhost",
})


def _has_phishing_prone_tld(url: str) -> bool:
    """Return True if ``url``'s registered TLD is in the phishing-prone list."""
    ext = _tld(url)
    if ext.suffix:
        return ext.suffix.lower() in _PHISHING_PRONE_TLDS
    # Fallback when tldextract doesn't recognise the suffix (reserved TLDs)
    host = url.split("/", 1)[0].lower()
    parts = host.split(".")
    return len(parts) >= 2 and parts[-1] in _PHISHING_PRONE_TLDS


def _looks_like_domain(candidate: str) -> bool:
    """Return True if ``candidate`` parses as a real registrable domain via
    the PSL snapshot, or falls back to an RFC 2606 reserved TLD. Filters out
    bare-word regex matches whose 'TLD' isn't actually a public suffix
    (``3.14`` -> suffix='14' -> rejected)."""
    if len(candidate) < _BARE_DOMAIN_MIN_LEN:
        return False
    ext = _tld(candidate)
    if ext.suffix and ext.domain:
        return True
    # Fallback for reserved TLDs (RFC 2606). tldextract's fallback behaviour
    # for unknown suffixes puts the last label in ``.domain`` with no
    # ``.suffix``, so we recover the last label ourselves from the host part.
    host = candidate.split("/", 1)[0].lower()
    parts = host.split(".")
    if len(parts) >= 2 and parts[-1] in _RFC2606_RESERVED_TLDS:
        return True
    return False


def _decode_datum_strings(datum: Any) -> List[str]:
    """Walk a Plutus-Data inline datum and collect every UTF-8 decodable
    text span >= 4 bytes. Handles two representations produced by ingestion:

      - hex-encoded CBOR string (the shape Ogmios v6 emits for most inline
        datums). Scanned at the byte level so we don't have to parse CBOR
        structure; UTF-8 strings inside Plutus Data sit contiguously in
        the blob and pop out as printable-ASCII runs.
      - nested dict in Ogmios' Plutus-Data-JSON representation
        (``{"bytes": "..."}``, ``{"list": [...]}``, ``{"map": [...]}``,
        ``{"constructor": n, "fields": [...]}``). Recurse and decode.
    """
    results: List[str] = []

    def _scan_bytes_for_strings(blob: bytes) -> None:
        start: Optional[int] = None
        for i, b in enumerate(blob):
            # Printable ASCII range; URLs are ASCII in practice.
            if 0x20 <= b < 0x7f:
                if start is None:
                    start = i
            else:
                if start is not None and i - start >= 4:
                    try:
                        results.append(blob[start:i].decode("utf-8"))
                    except UnicodeDecodeError:
                        pass
                start = None
        if start is not None and len(blob) - start >= 4:
            try:
                results.append(blob[start:].decode("utf-8"))
            except UnicodeDecodeError:
                pass

    def _walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, bytes):
            _scan_bytes_for_strings(node)
            return
        if isinstance(node, str):
            # Heuristic: long hex strings are typically CBOR-encoded datum
            # bodies. Decode + byte-scan. Non-hex strings we keep as-is so
            # plain text in nested dict values isn't lost.
            stripped = node.strip()
            if len(stripped) >= 8 and all(c in "0123456789abcdefABCDEF" for c in stripped):
                try:
                    _scan_bytes_for_strings(bytes.fromhex(stripped))
                    return
                except ValueError:
                    pass
            results.append(node)
            return
        if isinstance(node, dict):
            # Ogmios Plutus-Data-JSON node types
            if "bytes" in node and isinstance(node["bytes"], str):
                try:
                    raw = bytes.fromhex(node["bytes"])
                    _scan_bytes_for_strings(raw)
                except ValueError:
                    pass
                return
            if "list" in node and isinstance(node["list"], list):
                for item in node["list"]:
                    _walk(item)
                return
            if "map" in node and isinstance(node["map"], list):
                for entry in node["map"]:
                    if isinstance(entry, dict):
                        _walk(entry.get("k"))
                        _walk(entry.get("v"))
                return
            if "fields" in node and isinstance(node["fields"], list):
                for field in node["fields"]:
                    _walk(field)
                return
            # Fallback for generic dicts
            for k, v in node.items():
                _walk(k)
                _walk(v)
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return

    _walk(datum)
    return results


class PhishingScorer(BaseScorer):
    name = "phishing"

    def gate(self, features: Dict[str, Any]) -> bool:
        """Fire when phishing URLs appear in EITHER tx-level metadata
        (CIP-20 label 674 / CIP-25 label 721) OR an output's inline datum
        (CIP-68 reference-NFT pattern and similar datum-carried payloads).

        Sender allowlist: if any input address matches a known legitimate
        sender, skip scoring to reduce false positives (Polimi Section 4.9.4).
        """
        # Allowlist check
        addresses = features.get("addresses") or []
        if addresses and external.is_sender_allowlisted(addresses):
            return False

        # Either source of URLs can trigger the scorer.
        urls = self._extract_urls(features)
        return len(urls) > 0

    def score(self, features: Dict[str, Any]) -> ScorerResult:
        metadata = features.get("metadata") or {}
        urls = self._extract_urls(features)
        if not urls:
            return ScorerResult(score=0.0)

        # ----- Content sub-pipeline (weight = 0.65) -----

        # Sub-score 1a: URL blacklist match (weight 0.40 of content)
        s_blacklist = self._score_blacklist(urls)

        # Sub-score 1b: domain suspicion composite (weight 0.35 of content)
        s_domain = self._score_domain_suspicion(urls)

        # Sub-score 1c: social engineering score (weight 0.25 of content)
        s_social = self._score_social_engineering(features)

        content_score = (
            float(_W_CONTENT["blacklist"]) * s_blacklist
            + float(_W_CONTENT["domain"]) * s_domain
            + float(_W_CONTENT["social"]) * s_social
        )

        # ----- Delivery sub-pipeline (weight = 0.35) -----

        # Sub-score 2a: recipient_count (weight 0.35 of delivery)
        # Uses dynamic baselines with bootstrap fallback until Phase 2
        output_count = features.get("output_count", 0)
        network = features.get("network", "")
        p50, p99, bl_source = _resolve(
            "recipient_count", "global", "__global__", network,
            _BOOT, "recipient_count",
        )
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

        # Combo bonus: on-chain metadata that carries BOTH a URL AND Tier-2
        # phishing text is substantially more suspicious than either signal
        # alone. Individual signals are ambiguous (a bare URL could be a
        # link share; "claim your rewards" could be a legitimate staking
        # notification) but the pair is a textbook phishing move and rarely
        # appears in legitimate traffic. +0.25 pushes the clearer cases into
        # the High band without overreliance on blacklist / brand matching.
        if len(urls) > 0 and s_social >= float(_REASON_T["social"]):
            raw += 0.25

        # Additional bonus: phishing-prone TLDs (.xyz / .top / .click / ...
        # and RFC 2606 placeholders like .test / .example). Cardano protocols
        # don't live in these TLDs; a URL on one of them paired with Tier-2
        # text is very high-signal, so this bonus stacks on top of the URL
        # combo.
        if s_social >= float(_REASON_T["social"]) and any(
            _has_phishing_prone_tld(u) for u in urls
        ):
            raw += 0.15

        final_score = finalise_score(raw)

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

    def _extract_urls(self, features: Dict[str, Any]) -> List[str]:
        """Extract URL candidates from every known carrier and validate that
        each one has a real public-suffix TLD.

        Three carriers are scanned:

          1. Tx-level metadata under a relevant label (``674``, ``721``).
          2. Inline datums on outputs — walks the Plutus-Data tree and
             collects any UTF-8 decodable span. Catches CIP-68
             reference-NFT phishing (metadata lives in the datum, not in
             auxiliary data).
          3. Bare-domain forms (``cardano-drop.io/claim``) alongside
             scheme-prefixed URLs. Filtered through the PSL snapshot so
             bare-word matches like ``3.14`` don't survive.
        """
        candidates: List[str] = []

        # --- Carrier 1: tx-level metadata labels ------------------------
        metadata = features.get("metadata") or {}
        if isinstance(metadata, dict):
            for label_key in _RELEVANT_LABELS:
                content = metadata.get(label_key) or metadata.get(int(label_key))
                if content is None:
                    continue
                text = self._flatten_to_text(content)
                candidates.extend(_URL_RE.findall(text))
                candidates.extend(_BARE_DOMAIN_RE.findall(text))

        # --- Carrier 2: inline datums on outputs ------------------------
        raw_data = features.get("raw_data") or {}
        outputs = raw_data.get("outputs") if isinstance(raw_data, dict) else None
        if isinstance(outputs, list):
            for out in outputs:
                if not isinstance(out, dict):
                    continue
                datum = out.get("datum")
                if datum is None:
                    continue
                for decoded in _decode_datum_strings(datum):
                    candidates.extend(_URL_RE.findall(decoded))
                    candidates.extend(_BARE_DOMAIN_RE.findall(decoded))

        # Validate each candidate through tldextract's PSL. Scheme-prefixed
        # hits pass trivially; bare-domain hits only survive if their TLD
        # is a real public suffix.
        seen: set = set()
        validated: List[str] = []
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            if cand.lower().startswith(("http://", "https://")):
                validated.append(cand)
                continue
            if _looks_like_domain(cand):
                validated.append(cand)
        return validated

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

        Uses tldextract to isolate the registrable domain (`api.andamio.io` ->
        `andamio`, `foo.co.uk` -> `foo`) before Levenshtein comparison, so
        subdomain prefixes and multi-part TLDs don't pollute the signal.
        Exact matches on the registrable domain are skipped: a legitimate
        subdomain of a known protocol (`api.sundaeswap.finance`) should not
        be flagged against `sundaeswap.finance`.

        Domain age scoring requires WHOIS lookup (deferred to mainnet).
        """
        known_domains = external.get_protocol_domains()
        # Precompute legit (registrable_domain, brand) once per call. Cached
        # module-level would be better but the list may refresh via external.
        legit_info = [
            (_registrable_domain(d), _brand(d))
            for d in known_domains
        ]
        legit_info = [(r, b) for r, b in legit_info if r and b]
        max_brand_sim = 0.0

        for url in urls:
            tx_registrable = _registrable_domain(url)
            tx_brand = _brand(url)
            if not tx_registrable or not tx_brand:
                continue
            # Skip subdomains of legitimate domains: andamio.io is not
            # phishing sundaeswap.finance just because its subdomain
            # overlaps with another site's subdomain.
            if any(tx_registrable == lr for lr, _ in legit_info):
                continue
            tx_brand_lc = tx_brand.lower()
            for _, legit_brand in legit_info:
                sim = fuzz.ratio(tx_brand_lc, legit_brand.lower()) / 100.0
                if float(_SIM_RANGE["lo"]) < sim < float(_SIM_RANGE["hi"]):
                    max_brand_sim = max(max_brand_sim, sim)

        p50_b, p99_b = _anchor(_FIXED, "brand_sim")
        s_brand = normalise(max_brand_sim, p50=p50_b, p99=p99_b)

        # Domain age: requires WHOIS API (deferred to mainnet).
        # When domain age is available, composite = 0.50 * s_age + 0.50 * s_brand.
        # Until then, use brand similarity as the sole domain suspicion signal.
        return s_brand

    def _score_social_engineering(self, features: Dict[str, Any]) -> float:
        """Score tx-level metadata AND inline-datum text for social
        engineering patterns. Previously only scanned tx-level metadata;
        CIP-68 phishing hides its message body inside the reference NFT's
        inline datum, which the metadata-only scan would miss."""
        # Flatten tx-level metadata first
        metadata = features.get("metadata") or {}
        text = self._flatten_to_text(metadata).lower()

        # Append any UTF-8 decodable text spans found inside inline datums
        # on outputs. Same carriers the URL extractor walks.
        raw_data = features.get("raw_data") or {}
        outputs = raw_data.get("outputs") if isinstance(raw_data, dict) else None
        if isinstance(outputs, list):
            for out in outputs:
                if not isinstance(out, dict):
                    continue
                datum = out.get("datum")
                if datum is None:
                    continue
                for span in _decode_datum_strings(datum):
                    text += " " + span.lower()

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
