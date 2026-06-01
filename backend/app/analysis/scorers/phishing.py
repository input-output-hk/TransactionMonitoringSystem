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

import os
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

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
from app.analysis.features import get_cbor2
from app.config import settings

# No-network tldextract: use the PSL snapshot bundled with the wheel rather
# than hitting the network on first use. Safer for offline / sandboxed envs.
#
# Cache dir: the container runs as a non-root user with no home directory
# (Dockerfile uses ``--no-create-home``), so tldextract's default
# ``~/.cache/python-tldextract`` is unwritable and every init logs a
# "Permission denied" warning + re-loads the snapshot. Point the cache at
# a writable dir derived from RAW_STORE_PATH (a mounted, appuser-owned
# volume in Docker). Best-effort: if the dir can't be created we fall back
# to ``cache_dir=None`` (snapshot-only, no disk persistence) so the scorer
# never fails to import over a cache-path problem.
def _build_tld_extractor() -> tldextract.TLDExtract:
    try:
        cache_dir = os.path.join(settings.RAW_STORE_PATH, "tldextract")
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        cache_dir = None
    return tldextract.TLDExtract(
        suffix_list_urls=(),
        fallback_to_snapshot=True,
        cache_dir=cache_dir,
    )


_tld = _build_tld_extractor()


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
_URL_COMBO_BONUS = float(_SE["url_combo_bonus"])
_PHISHING_TLD_BONUS = float(_SE["phishing_tld_bonus"])
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


def _url_host(url: str) -> str:
    """Return the lowercase host portion of ``url``, stripping any
    ``scheme://`` prefix and trailing ``/path`` / ``?query``. Mirrors what
    ``tldextract`` would feed its parser, but produced manually so callers
    can also operate on inputs that tldextract doesn't recognise (e.g.
    RFC 2606 reserved TLDs like ``.test``).
    """
    after_scheme = url.split("://", 1)[-1]
    return after_scheme.split("/", 1)[0].split("?", 1)[0].lower()


def _has_phishing_prone_tld(url: str) -> bool:
    """Return True if ``url``'s registered TLD is in the phishing-prone list."""
    ext = _tld(url)
    if ext.suffix:
        return ext.suffix.lower() in _PHISHING_PRONE_TLDS
    # Fallback when tldextract doesn't recognise the suffix (RFC 2606
    # reserved TLDs: .test / .example / .invalid / .localhost). The PSL
    # path above handles every routable TLD; this branch only fires for
    # placeholders, so the manual host split is safe enough.
    host = _url_host(url)
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
    text span. Handles two representations produced by ingestion:

      - hex-encoded CBOR string (the shape Ogmios v6 emits for most inline
        datums). Decoded via cbor2 so map/list/constructor structure is
        preserved and each leaf bytes/text value comes out cleanly; a
        previous byte-scan implementation concatenated adjacent values
        because CBOR length-prefix bytes (``0x40``-``0x57`` for byte
        strings) fall in printable ASCII and looked like part of the
        next text run, producing strings like
        ``walletEimageTclaim-reward-ada.xyz``.
      - nested dict in Ogmios' Plutus-Data-JSON representation
        (``{"bytes": "..."}``, ``{"list": [...]}``, ``{"map": [...]}``,
        ``{"constructor": n, "fields": [...]}``). Recurse and decode.
    """
    results: List[str] = []

    _MIN_LEN = 4

    def _emit_bytes(blob: bytes) -> None:
        """Try to UTF-8 decode and emit; fall back to a byte-scan of
        printable-ASCII runs when the blob isn't valid UTF-8."""
        try:
            decoded = blob.decode("utf-8")
        except UnicodeDecodeError:
            _scan_bytes_for_strings(blob)
            return
        if len(decoded) >= _MIN_LEN:
            results.append(decoded)

    def _scan_bytes_for_strings(blob: bytes) -> None:
        """Last-resort printable-ASCII scan. Only used when a byte
        string isn't valid UTF-8 so cbor2 / direct decode can't surface
        it cleanly."""
        start: Optional[int] = None
        for i, b in enumerate(blob):
            if 0x20 <= b < 0x7f:
                if start is None:
                    start = i
            else:
                if start is not None and i - start >= _MIN_LEN:
                    try:
                        results.append(blob[start:i].decode("utf-8"))
                    except UnicodeDecodeError:
                        pass
                start = None
        if start is not None and len(blob) - start >= _MIN_LEN:
            try:
                results.append(blob[start:].decode("utf-8"))
            except UnicodeDecodeError:
                pass

    def _walk_cbor(node: Any) -> None:
        """Walk the cbor2-parsed structure. CBOR text strings come out
        as ``str``, byte strings as ``bytes``, maps as ``dict``, arrays
        as ``list``, and Plutus-Data constructors as ``cbor2.CBORTag``
        whose ``.value`` is the fields array."""
        if isinstance(node, bytes):
            _emit_bytes(node)
            return
        if isinstance(node, str):
            if len(node) >= _MIN_LEN:
                results.append(node)
            return
        if isinstance(node, dict):
            for k, v in node.items():
                _walk_cbor(k)
                _walk_cbor(v)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                _walk_cbor(item)
            return
        # cbor2.CBORTag has ``.value``; duck-type to avoid an import
        # dependency at this module's top.
        inner = getattr(node, "value", None)
        if inner is not None and not isinstance(node, (int, float, bool)):
            _walk_cbor(inner)

    def _try_cbor(blob: bytes) -> bool:
        """Best-effort CBOR parse + structural walk.

        Returns True if cbor2 parsed the blob and the walk completed,
        regardless of whether any string leaves were appended (a numeric-
        only blob is still considered "successfully handled" — the byte
        scan wouldn't find anything either). Returns False if cbor2 is
        unavailable or the blob isn't valid CBOR, so the caller can fall
        back to the printable-ASCII scan for untyped payloads.
        """
        try:
            cbor2 = get_cbor2()
        except Exception:
            return False
        try:
            decoded = cbor2.loads(blob)
        except Exception:
            return False
        _walk_cbor(decoded)
        return True

    def _walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, bytes):
            if not _try_cbor(node):
                _emit_bytes(node)
            return
        if isinstance(node, str):
            # Long hex strings are typically CBOR-encoded datum bodies.
            # Decode and walk the parsed CBOR so each leaf string comes
            # out cleanly. Falls back to the byte-scan if cbor2 can't
            # parse it. Non-hex strings we keep as-is.
            stripped = node.strip()
            if len(stripped) >= 8 and all(c in "0123456789abcdefABCDEF" for c in stripped):
                try:
                    raw = bytes.fromhex(stripped)
                except ValueError:
                    raw = None
                if raw is not None:
                    if not _try_cbor(raw):
                        _emit_bytes(raw)
                    return
            if len(node) >= _MIN_LEN:
                results.append(node)
            return
        if isinstance(node, dict):
            # Ogmios Plutus-Data-JSON node types. Inside this shape, a
            # ``{"bytes": ...}`` node is a *leaf* byte-string already
            # disentangled from its enclosing CBOR — never re-parse it as
            # CBOR (cbor2 would happily interpret an ASCII URL like
            # ``https://...`` as a random text-string + trailing garbage).
            if "bytes" in node and isinstance(node["bytes"], str):
                try:
                    raw = bytes.fromhex(node["bytes"])
                except ValueError:
                    return
                _emit_bytes(raw)
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
        s_social, se_tier = self._classify_social_engineering(features)

        content_score = (
            float(_W_CONTENT["blacklist"]) * s_blacklist
            + float(_W_CONTENT["domain"]) * s_domain
            + float(_W_CONTENT["social"]) * s_social
        )

        # ----- Delivery sub-pipeline (weight = 0.35) -----

        # Sub-score 2a: recipient_count (weight 0.35 of delivery)
        # Uses dynamic baselines with bootstrap fallback until Phase 2.
        # Count distinct recipient addresses, not raw output_count: a single
        # recipient receiving many micro-outputs in one tx inflates
        # output_count and produced false positives on consolidation /
        # change-splitting patterns. The evidence panel reads the same
        # value, so the donut and the drill-down stay aligned.
        network = features.get("network", "")
        raw_data_field = features.get("raw_data")
        raw_outputs = (
            raw_data_field.get("outputs") or []
            if isinstance(raw_data_field, dict)
            else []
        )
        distinct_recipients = len({
            o.get("address", "")
            for o in raw_outputs
            if isinstance(o, dict) and o.get("address")
        })
        # Fall back to output_count when raw_data isn't a dict (e.g. some
        # ingestion paths persist it as JSON string before normalisation),
        # so we never score zero recipients on a tx with real outputs.
        recipient_count = distinct_recipients or int(features.get("output_count", 0) or 0)
        p50, p99, bl_source = _resolve(
            "recipient_count", "global", "__global__", network,
            _BOOT, "recipient_count",
        )
        s_recipients = normalise(recipient_count, p50=p50, p99=p99)

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
        # appears in legitimate traffic. The bonus pushes the clearer cases
        # into the High band without overreliance on blacklist / brand
        # matching. Magnitude tunable via phishing.social_engineering.
        if len(urls) > 0 and s_social >= float(_REASON_T["social"]):
            raw += _URL_COMBO_BONUS

        # Additional bonus: phishing-prone TLDs (.xyz / .top / .click / ...
        # and RFC 2606 placeholders like .test / .example). Cardano protocols
        # don't live in these TLDs; a URL on one of them paired with Tier-2
        # text is very high-signal, so this bonus stacks on top of the URL
        # combo. Magnitude tunable via phishing.social_engineering.
        if s_social >= float(_REASON_T["social"]) and any(
            _has_phishing_prone_tld(u) for u in urls
        ):
            raw += _PHISHING_TLD_BONUS

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

        blacklist_patterns = external.get_phishing_patterns()
        metadata_labels = sorted(
            {str(k) for k in (metadata or {}).keys()}
        ) if isinstance(metadata, dict) else []

        url_records = []
        for url in urls:
            is_blacklisted = any(p.search(url) for p in blacklist_patterns)
            phishing_tld = _has_phishing_prone_tld(url)
            if is_blacklisted:
                url_severity = "BLACKLISTED"
            elif phishing_tld:
                url_severity = "SUSPICIOUS"
            else:
                url_severity = "NORMAL"
            url_records.append({
                "url": url,
                "severity": url_severity,
                "phishing_tld": phishing_tld,
            })

        return ScorerResult(
            score=final_score,
            sub_scores=sub_scores,
            reasons=reasons,
            baseline_source=bl_source,
            severity=severity,
            evidence={
                "severity": severity,
                "se_tier": se_tier,
                "urls": url_records,
                "url_count": len(urls),
                "recipient_count": recipient_count,
                "metadata_labels": metadata_labels,
            },
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

        # Collapse bare-domain duplicates that are already represented in
        # their scheme-prefixed form (CIP-20 messages often carry both
        # ``https://x.app/path`` AND ``x.app/path`` because the bare regex
        # also matches the part after the scheme). The operator sees one
        # URL per real link instead of two.
        scheme_strip = {
            u[len("https://"):] if u.lower().startswith("https://")
            else u[len("http://"):] if u.lower().startswith("http://")
            else None
            for u in validated
        }
        scheme_strip.discard(None)
        return [
            u for u in validated
            if u.lower().startswith(("http://", "https://"))
            or u not in scheme_strip
        ]

    def _flatten_to_text(self, obj: Any) -> str:
        """Recursively flatten a metadata value to a single string.

        CIP-20 stores long values as arrays of <=64-byte text chunks that the
        spec defines as concatenated without separators (the chunking is a
        CBOR-encoding workaround, not a content boundary). When a list is
        purely strings, join with ``""`` so URLs split across chunks
        reconstitute correctly; otherwise fall back to space-joining so
        nested structures still render readably for the SE-tier regex pass.
        """
        if isinstance(obj, str):
            return obj
        if isinstance(obj, list):
            if all(isinstance(item, str) for item in obj):
                return "".join(obj)
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
        engineering patterns. Thin wrapper that discards the tier label;
        the actual classification lives in ``_classify_social_engineering``
        so the evidence path can read it without recomputing.
        """
        score, _tier = self._classify_social_engineering(features)
        return score

    def _classify_social_engineering(
        self, features: Dict[str, Any],
    ) -> Tuple[float, str]:
        """Return ``(normalised_score, tier_label)`` from the social-engineering
        text scan. The tier label names the highest matching tier and is
        surfaced in evidence so operators see "Tier 1: Credential harvesting"
        on the detail page rather than only the donut percentage.

        Tier hierarchy follows Polimi Section 4.9.3:
          - Tier 1: near-deterministic credential-request phrasing.
          - Tier 2: urgency language ("act now", "verify immediately").
          - Tier 3: brand impersonation in suspicious context.
          - None:   no match.
        """
        metadata = features.get("metadata") or {}
        text = self._flatten_to_text(metadata).lower()

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
            return 0.0, "None"

        # Tier 1 short-circuits the rest: a credential-request match is
        # near-deterministic so we don't bother counting urgency or brand.
        for pattern in external.TIER1_CREDENTIAL_PATTERNS:
            if pattern.lower() in text:
                return 1.0, "Tier 1: Credential harvesting"

        score = 0.0
        tiers_hit: List[str] = []

        urgency_matches = sum(
            1 for pattern in external.TIER2_URGENCY_PATTERNS
            if re.search(pattern, text, re.IGNORECASE)
        )
        if urgency_matches > 0:
            score += min(
                float(_SE["urgency_cap"]),
                urgency_matches * float(_SE["urgency_increment"]),
            )
            tiers_hit.append("Tier 2: Urgency language")

        brand_matches = sum(
            1 for brand in external.TIER3_BRAND_NAMES
            if brand.lower() in text
        )
        if brand_matches > 0:
            score += min(
                float(_SE["brand_cap"]),
                brand_matches * float(_SE["brand_increment"]),
            )
            tiers_hit.append("Tier 3: Brand impersonation")

        p50_s, p99_s = _anchor(_FIXED, "social_score")
        normalised_score = normalise(score, p50=p50_s, p99=p99_s)
        tier_label = " + ".join(tiers_hit) if tiers_hit else "None"
        return normalised_score, tier_label
