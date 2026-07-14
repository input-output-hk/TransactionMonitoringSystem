"""URL extraction and validation for on-chain phishing payloads.

Split out of the phishing scorer: everything here is generic
"find and vet URLs in attacker-controlled text" machinery (regexes,
defang normalisation, PSL validation, TLD reputation), independent of the
scoring pipeline that consumes it.
"""

import logging
import os
import re
from typing import List, Optional

import tldextract

from app.config import settings

logger = logging.getLogger(__name__)


# No-network tldextract: use the PSL snapshot bundled with the wheel rather
# than hitting the network on first use. Safer for offline / sandboxed envs.
#
# Cache dir: the container runs as a non-root user with no home directory
# (Dockerfile uses ``--no-create-home``), so tldextract's default
# ``~/.cache/python-tldextract`` is unwritable and every init logs a
# "Permission denied" warning + re-loads the snapshot. Point the cache at
# a writable dir derived from RAW_STORE_PATH (a mounted, appuser-owned
# volume in Docker). Best-effort: if the dir can't be created we fall back
# to ``cache_dir=None`` (snapshot-only, no disk persistence) so the module
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


def registrable_domain(url_or_domain: str) -> Optional[str]:
    """Return the registrable domain (brand + public suffix), e.g.
    'api.andamio.io' -> 'andamio.io', 'foo.co.uk' -> 'foo.co.uk'.
    Returns None for IP addresses or unparseable input."""
    ext = _tld(url_or_domain)
    if not ext.domain or not ext.suffix:
        return None  # IP address, localhost, or non-domain
    return f"{ext.domain}.{ext.suffix}"


def brand(url_or_domain: str) -> Optional[str]:
    """Return the brand (registrable domain minus public suffix)."""
    ext = _tld(url_or_domain)
    return ext.domain or None


# URL extraction regexes.
#
# URL_RE: strict http(s) URL form. Always preferred when a scheme is present.
# BARE_DOMAIN_RE: 2+ dot-separated DNS labels with an optional path. Used to
#   catch scheme-less phishing payloads like ``cardano-drop.io/claim`` that
#   CIP-20 messages routinely carry. Matches get validated against
#   tldextract's PSL snapshot (see ``looks_like_domain``) so bare-word
#   constructs like ``3.14`` or ``version.py`` don't produce false positives.
URL_RE = re.compile(
    r'https?://[^\s"\'<>\]\)}{,]+',
    re.IGNORECASE,
)
BARE_DOMAIN_RE = re.compile(
    r'\b(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+[a-z]{2,24}(?:/[^\s"\'<>\]\)}{,]*)?',
    re.IGNORECASE,
)

# Minimum candidate length to consider, post-TLD-validation. Rules out very
# short matches where tldextract's PSL recognises a 2-letter TLD (e.g. ``a.io``
# is technically parseable but almost always noise).
BARE_DOMAIN_MIN_LEN = 6

# Defanged-URL normalisation, applied to carrier text BEFORE regex
# extraction. Phishing payloads routinely "defang" URLs to dodge naive
# scanners: bracketed/parenthesised/spelled dots (``evil[.]io``,
# ``evil(.)io``, ``evil[dot]io``), Unicode dot lookalikes (ideographic
# full stop U+3002, fullwidth U+FF0E, halfwidth U+FF61), and ``hxxp``
# scheme mangling. These are notation conventions, not tunables, so they
# live here as a named table rather than in detection.yaml.
DEFANG_REPLACEMENTS = (
    ("[.]", "."),
    ("(.)", "."),
    ("[dot]", "."),
    ("(dot)", "."),
    ("。", "."),
    ("．", "."),
    ("｡", "."),
)
_HXXP_RE = re.compile(r"hxxp(s?)://", re.IGNORECASE)


def refang(text: str) -> str:
    """Undo common URL defanging so the extraction regexes see the real URL."""
    for needle, repl in DEFANG_REPLACEMENTS:
        text = text.replace(needle, repl)
    return _HXXP_RE.sub(r"http\1://", text)


def url_candidates(text: str) -> List[str]:
    """Raw URL + bare-domain regex hits in ``text`` (defang-normalised,
    un-validated)."""
    text = refang(text)
    return URL_RE.findall(text) + BARE_DOMAIN_RE.findall(text)


# RFC 2606 reserved TLDs. Not in Mozilla's Public Suffix List (and so rejected
# by tldextract) but frequently appear in phishing-harness output and the
# occasional real on-chain simulation. Accepting them closes a detection gap
# without meaningfully expanding the FP surface — these TLDs don't resolve,
# so any on-chain URL using them is almost certainly test / simulation /
# deliberate fake.
RFC2606_RESERVED_TLDS = frozenset({"test", "example", "invalid", "localhost"})

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
PHISHING_PRONE_TLDS = frozenset(
    {
        # Cheap / bulk / free registration
        "xyz",
        "top",
        "click",
        "link",
        "live",
        "online",
        "site",
        "space",
        "loan",
        "download",
        "stream",
        "tk",
        "ml",
        "ga",
        "cf",
        "gdn",
        "work",
        "party",
        "trade",
        "date",
        "science",
        # RFC 2606 reserved — non-routable, placeholder use only
        "test",
        "example",
        "invalid",
        "localhost",
    }
)


def url_host(url: str) -> str:
    """Return the lowercase host portion of ``url``, stripping any
    ``scheme://`` prefix and trailing ``/path`` / ``?query``. Mirrors what
    ``tldextract`` would feed its parser, but produced manually so callers
    can also operate on inputs that tldextract doesn't recognise (e.g.
    RFC 2606 reserved TLDs like ``.test``).
    """
    after_scheme = url.split("://", 1)[-1]
    return after_scheme.split("/", 1)[0].split("?", 1)[0].lower()


def has_phishing_prone_tld(url: str) -> bool:
    """Return True if ``url``'s registered TLD is in the phishing-prone list."""
    ext = _tld(url)
    if ext.suffix:
        return ext.suffix.lower() in PHISHING_PRONE_TLDS
    # Fallback when tldextract doesn't recognise the suffix (RFC 2606
    # reserved TLDs: .test / .example / .invalid / .localhost). The PSL
    # path above handles every routable TLD; this branch only fires for
    # placeholders, so the manual host split is safe enough.
    host = url_host(url)
    parts = host.split(".")
    return len(parts) >= 2 and parts[-1] in PHISHING_PRONE_TLDS


def looks_like_domain(candidate: str) -> bool:
    """Return True if ``candidate`` parses as a real registrable domain via
    the PSL snapshot, or falls back to an RFC 2606 reserved TLD. Filters out
    bare-word regex matches whose 'TLD' isn't actually a public suffix
    (``3.14`` -> suffix='14' -> rejected)."""
    if len(candidate) < BARE_DOMAIN_MIN_LEN:
        return False
    ext = _tld(candidate)
    if ext.suffix and ext.domain:
        return True
    # Fallback for reserved TLDs (RFC 2606). tldextract's fallback behaviour
    # for unknown suffixes puts the last label in ``.domain`` with no
    # ``.suffix``, so we recover the last label ourselves from the host part.
    host = candidate.split("/", 1)[0].lower()
    parts = host.split(".")
    if len(parts) >= 2 and parts[-1] in RFC2606_RESERVED_TLDS:
        return True
    return False


def validate_candidates(candidates: List[str]) -> List[str]:
    """Validate each candidate through tldextract's PSL. Scheme-prefixed
    hits pass trivially; bare-domain hits only survive if their TLD
    is a real public suffix."""
    seen: set = set()
    validated: List[str] = []
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if cand.lower().startswith(("http://", "https://")):
            validated.append(cand)
            continue
        if looks_like_domain(cand):
            validated.append(cand)

    # Collapse bare-domain duplicates that are already represented in
    # their scheme-prefixed form (CIP-20 messages often carry both
    # ``https://x.app/path`` AND ``x.app/path`` because the bare regex
    # also matches the part after the scheme). The operator sees one
    # URL per real link instead of two.
    scheme_strip = {
        u[len("https://") :]
        if u.lower().startswith("https://")
        else u[len("http://") :]
        if u.lower().startswith("http://")
        else None
        for u in validated
    }
    scheme_strip.discard(None)
    return [
        u
        for u in validated
        if u.lower().startswith(("http://", "https://")) or u not in scheme_strip
    ]
