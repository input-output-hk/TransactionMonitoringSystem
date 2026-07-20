"""Phishing attack scorer (Class 9).

Detects malicious URLs, social engineering content, and deceptive instructions
embedded in on-chain transaction metadata (CIP-0020 label 674, CIP-0025 label
721), in inline datums, or in native-asset names.  Scores via two
sub-pipelines:

  Content   (weight 0.65): URL blacklist match, domain suspicion, social
                           engineering keyword patterns.
  Delivery  (weight 0.35): mass distribution indicators (output count,
                           recipient uniformity, URL recurrence).

Gate condition: at least one URL extracted from any carrier (relevant
metadata label, inline datum, or decoded asset name).
"""

import logging
import re
from typing import Any

from rapidfuzz import fuzz

from app.analysis import external
from app.analysis.features import decode_hex_asset_name
from app.analysis.normalise import normalise
from app.analysis.plutus_text import decode_datum_strings
from app.analysis.scorer_config import (
    anchor as _anchor,
)
from app.analysis.scorer_config import (
    get as _get_cfg,
)
from app.analysis.scorer_config import (
    resolved_or_bootstrap as _resolve,
)
from app.analysis.scorers.base import (
    FUZZ_RATIO_SCALE,
    BaseScorer,
    ScorerResult,
    finalise_score,
)
from app.analysis.url_extraction import (
    brand as _brand,
)
from app.analysis.url_extraction import (
    has_phishing_prone_tld as _has_phishing_prone_tld,
)
from app.analysis.url_extraction import (
    registrable_domain as _registrable_domain,
)
from app.analysis.url_extraction import (
    url_candidates as _url_candidates,
)
from app.analysis.url_extraction import (
    validate_candidates as _validate_candidates,
)

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
_ASSET_CARRIER_ENABLED = bool(_CFG["asset_name_carrier"]["enabled"])
# Withhold the two stacking bonuses when asset names are the only URL
# carrier and the tokens are positively proven pure self-change (see
# _url_token_delivery_stats and the gating block in score()).
_REQUIRE_DELIVERY_FOR_BONUSES = bool(_CFG["asset_name_carrier"]["require_delivery_for_bonuses"])
# Minimum length for a decoded datum text span to be kept for URL / SE
# scanning. Shorter spans are CBOR structural noise, not content.
_MIN_DECODED_STR_LEN = int(_CFG["min_decoded_string_len"])

# Maximum nesting depth when flattening a metadata value to text. Metadata is
# attacker-controlled; an unbounded recursive flatten lets a deeply-nested
# value raise RecursionError, which the engine's per-scorer try/except
# swallows, silently scoring phishing -1 (a recall-evasion primitive). CIP-20
# messages and CIP-25 metadata are shallow in practice, so 32 is far beyond any
# legitimate shape while making the walk always terminate.
_MAX_METADATA_FLATTEN_DEPTH = 32


def _decode_datum_strings(datum: Any) -> list[str]:
    """Datum text spans for the URL / social-engineering scans.

    Thin delegate to :func:`app.analysis.plutus_text.decode_datum_strings`
    binding this scorer's configured minimum span length; kept under the
    historical name because it is part of this module's test surface.
    """
    return decode_datum_strings(datum, _MIN_DECODED_STR_LEN)


def _decode_asset_name_strings(raw_data: Any) -> list[str]:
    """Collect decoded (hex -> UTF-8) native-asset names from a tx's mint map
    and output value bundles.

    The dominant in-the-wild Cardano phishing shape delivers the URL in the
    token name itself (a token literally named ``claim-ada.xyz``), airdropped
    to wallet addresses with NO metadata and NO datum, so it never enters the
    other two carriers. Both the ``mint`` map and every output ``value``
    bundle are scanned: an airdrop tx usually carries the name in both, but a
    re-distribution of a previously minted scam token only shows in outputs.

    Skips the ``ada`` (Ogmios v6) and ``lovelace`` (v5) keys when iterating
    value bundles. Asset names that do not decode as UTF-8 fall back to their
    raw hex form, which contains no ``.`` and therefore can never satisfy the
    downstream URL/domain matching.
    """
    if not isinstance(raw_data, dict):
        return []
    names: list[str] = []
    seen: set = set()

    def _collect(bundle: Any) -> None:
        if not isinstance(bundle, dict):
            return
        for policy_key, token_map in bundle.items():
            if policy_key in ("ada", "lovelace") or not isinstance(token_map, dict):
                continue
            for hex_name in token_map.keys():
                decoded = decode_hex_asset_name(str(hex_name))
                if decoded not in seen:
                    seen.add(decoded)
                    names.append(decoded)

    _collect(raw_data.get("mint"))
    outputs = raw_data.get("outputs")
    if isinstance(outputs, list):
        for out in outputs:
            if isinstance(out, dict):
                _collect(out.get("value"))
    return names


def _sender_addresses(features: dict[str, Any]) -> list[str]:
    """Resolved input (sender) addresses for this tx, taken from the enriched
    raw payload (``enrich_inputs_with_resolved_addresses`` writes the resolved
    sender into ``inputs[i]["address"]``).

    Output / recipient addresses are deliberately excluded: the sender
    allowlist may suppress only known-legitimate SENDERS. Including outputs let
    an attacker silence all phishing detection simply by paying an allowlisted
    protocol address as a recipient. Unresolved inputs contribute nothing, so a
    tx whose senders are unknown is never suppressed (fail open toward
    detection, recall-first).
    """
    raw_data = features.get("raw_data")
    if not isinstance(raw_data, dict):
        return []
    senders: list[str] = []
    for inp in raw_data.get("inputs", []) or []:
        if isinstance(inp, dict):
            addr = inp.get("address")
            if isinstance(addr, str) and addr:
                senders.append(addr)
    return senders


def _url_token_delivery_stats(raw_data: Any) -> tuple[int, int]:
    """``(total, net_new)`` counts over (URL-named asset, recipient) pairs.

    A pair is one distinct URL-bearing ``(policy_id, asset_name_hex)``
    reaching one distinct output address. The pair is *net-new* when the
    asset is minted in this tx (mint quantity > 0; an unparseable quantity
    counts as minted, toward detection) OR the recipient address did not
    already hold that exact asset in this tx's inputs (``inp["value"]``,
    attached by ``enrich_inputs_with_resolved_addresses``).

    Fail-open is structural, not a flag: classifying a pair as self-change
    requires POSITIVE proof of prior holding, so a missing input ``value``
    (originating tx absent from the warehouse, or the batch exceeded
    ``ANALYSIS_MAX_REF_TXS``), a missing input ``address``, or an absent
    ``inputs`` list can only shrink the prior-holder set and push the pair
    toward net-new (fail open toward detection, recall-first).

    Matching is on the FULL output address, not the stake credential: an
    attacker can mint an address from their own payment credential plus the
    victim's stake credential and pre-hold the token there, so stake-cred
    matching would let them fake self-change; holding the token at the
    victim's exact address as a *spent input* requires the victim's own
    witness. Known limitation: a wallet consolidating to a different
    self-owned address counts as net-new (entity clustering, when it lands,
    is the fix).
    """
    if not isinstance(raw_data, dict):
        return 0, 0

    url_named: dict[str, bool] = {}

    def _is_url_named(hex_name: str) -> bool:
        if hex_name not in url_named:
            decoded = decode_hex_asset_name(hex_name)
            url_named[hex_name] = bool(_validate_candidates(_url_candidates(decoded)))
        return url_named[hex_name]

    def _url_assets_in(bundle: Any):
        if not isinstance(bundle, dict):
            return
        for policy, token_map in bundle.items():
            if policy in ("ada", "lovelace") or not isinstance(token_map, dict):
                continue
            for hex_name, qty in token_map.items():
                if _is_url_named(str(hex_name)):
                    yield (str(policy), str(hex_name)), qty

    minted: set[tuple[str, str]] = set()
    for key, qty in _url_assets_in(raw_data.get("mint")):
        try:
            if int(qty) <= 0:
                # A pure burn cannot deliver; do not let it override the
                # prior-holding proof for the amount still self-changing.
                continue
        except (TypeError, ValueError):
            pass  # unparseable quantity counts as minted (toward detection)
        minted.add(key)

    prior_holders: dict[tuple[str, str], set[str]] = {}
    for inp in raw_data.get("inputs") or []:
        if not isinstance(inp, dict):
            continue
        addr = inp.get("address")
        if not isinstance(addr, str) or not addr:
            continue
        for key, _qty in _url_assets_in(inp.get("value")):
            prior_holders.setdefault(key, set()).add(addr)

    pairs: set[tuple[tuple[str, str], str]] = set()
    for out in raw_data.get("outputs") or []:
        if not isinstance(out, dict):
            continue
        addr = out.get("address")
        if not isinstance(addr, str) or not addr:
            continue  # unattributable recipient: no pair to classify
        for key, _qty in _url_assets_in(out.get("value")):
            pairs.add((key, addr))

    total = len(pairs)
    net_new = sum(
        1 for key, addr in pairs if key in minted or addr not in prior_holders.get(key, ())
    )
    return total, net_new


class PhishingScorer(BaseScorer):
    name = "phishing"

    def gate(self, features: dict[str, Any]) -> bool:
        """Fire when phishing URLs appear in tx-level metadata (CIP-20 label
        674 / CIP-25 label 721), in an output's inline datum (CIP-68
        reference-NFT pattern and similar datum-carried payloads), or in a
        decoded native-asset name (URL-named scam-token airdrops).

        Sender allowlist: if any RESOLVED INPUT (sender) address matches a known
        legitimate sender, skip scoring to reduce false positives (Polimi
        Section 4.9.4). Recipient (output) addresses never suppress, so an
        attacker cannot disable detection by paying an allowlisted address.
        """
        # Allowlist check: senders only (never recipients).
        senders = _sender_addresses(features)
        if senders and external.is_sender_allowlisted(senders):
            return False

        # A URL in any carrier triggers the scorer.
        urls = self._extract_urls(features)
        if urls:
            return True

        # Recall-first: a URL-less social-engineering message must still be
        # scored. A "send your seed phrase to ..." (Tier 1) or urgency/brand
        # bait carries no link but is itself the phishing signal (Polimi
        # 4.9.3); gating only on URL presence made the Tier-1 credential
        # detector unreachable. Open the gate on any non-zero SE signal.
        s_social, _ = self._classify_social_engineering(features)
        return s_social > 0.0

    def score(self, features: dict[str, Any]) -> ScorerResult:
        metadata = features.get("metadata") or {}
        urls = self._extract_urls(features)
        # Sub-score 1c computed up front: it is also the gate's URL-less
        # trigger, so a text-only social-engineering message (no URL) must
        # still be scored rather than short-circuited to 0.
        s_social, se_tier = self._classify_social_engineering(features)
        if not urls and s_social <= 0.0:
            return ScorerResult(score=0.0)

        # URLs delivered inside asset names, tracked separately for the
        # reason flag and evidence panel. Always a subset of ``urls``.
        asset_name_urls = (
            _validate_candidates(self._asset_name_candidates(features))
            if _ASSET_CARRIER_ENABLED
            else []
        )

        # ----- Content sub-pipeline (weight = 0.65) -----

        # Sub-score 1a: URL blacklist match (weight 0.40 of content)
        s_blacklist = self._score_blacklist(urls)

        # Sub-score 1b: domain suspicion composite (weight 0.35 of content)
        s_domain = self._score_domain_suspicion(urls)

        # Sub-score 1c (s_social / se_tier) already computed above.

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
            raw_data_field.get("outputs") or [] if isinstance(raw_data_field, dict) else []
        )
        distinct_recipients = len(
            {o.get("address", "") for o in raw_outputs if isinstance(o, dict) and o.get("address")}
        )
        # Fall back to output_count when raw_data isn't a dict (e.g. some
        # ingestion paths persist it as JSON string before normalisation),
        # so we never score zero recipients on a tx with real outputs.
        recipient_count = distinct_recipients or int(features.get("output_count", 0) or 0)
        p50, p99, bl_source = _resolve(
            "recipient_count",
            "global",
            "__global__",
            network,
            _BOOT,
            "recipient_count",
        )
        s_recipients = normalise(recipient_count, p50=p50, p99=p99)

        # Sub-score 2b: url_hash_recurrence (weight 0.25 of delivery)
        # Requires cross-tx URL indexing (deferred to mainnet)
        s_url_recur = 0.0

        # Sub-score 2c: targeting_score (weight 0.25 of delivery)
        # Net-new-holder delivery: the fraction of (URL-named asset,
        # recipient) pairs where the recipient did not already hold that
        # exact asset in this tx's inputs. A real URL-token airdrop is all
        # net-new (1.0); a URL-named token riding in the sender's own
        # change is 0.0. Already in [0, 1], so no anchor/normalise pass.
        # (The spec's recipient-to-protocol interaction graph remains
        # deferred; this is the delivery-side targeting signal.)
        url_pairs_total, url_pairs_net_new = (
            _url_token_delivery_stats(raw_data_field) if _ASSET_CARRIER_ENABLED else (0, 0)
        )
        s_targeting = (url_pairs_net_new / url_pairs_total) if url_pairs_total > 0 else 0.0

        # Sub-score 2d: sender_recurrence (weight 0.15 of delivery)
        # Requires entity clustering (deferred to mainnet)
        s_recurrence = 0.0

        # Spec weights: recipients 0.35, url_recur 0.25, targeting 0.25,
        # recurrence 0.15. Sub-scores 2b and 2d are deferred (0.0).
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

        # Bonus gating on delivery: when asset names are the SOLE URL
        # carrier and every (URL-asset, recipient) pair is positively proven
        # self-change (the recipient already held the exact asset in this
        # tx's inputs, nothing minted), the tx is a holder moving their own
        # bag, not a delivery, and the two stacking bonuses are withheld.
        # Suppression requires positive proof: any net-new pair, any minted
        # URL token, or any missing input value keeps the bonuses (fail open
        # toward detection). Metadata/datum-carried URLs never gate: a
        # "claim rewards at evil.xyz" message is phishing regardless of
        # token movement. Kill switch:
        # phishing.asset_name_carrier.require_delivery_for_bonuses.
        # "Asset names are the SOLE URL carrier" is decided from the
        # metadata/datum carriers directly, NOT by subtracting asset_name_urls
        # from the full set: a URL that appears in both a metadata message and
        # a self-change asset name is still metadata-delivered and must keep
        # its bonuses (string-differencing would have wrongly treated it as
        # asset-only and suppressed a genuine metadata phishing delivery).
        metadata_datum_urls = (
            _validate_candidates(self._metadata_datum_url_candidates(features))
            if asset_name_urls
            else urls
        )
        suppress_bonuses = (
            _REQUIRE_DELIVERY_FOR_BONUSES
            and bool(asset_name_urls)
            and not metadata_datum_urls
            and url_pairs_total > 0
            and url_pairs_net_new == 0
        )

        # Combo bonus: on-chain metadata that carries BOTH a URL AND Tier-2
        # phishing text is substantially more suspicious than either signal
        # alone. Individual signals are ambiguous (a bare URL could be a
        # link share; "claim your rewards" could be a legitimate staking
        # notification) but the pair is a textbook phishing move and rarely
        # appears in legitimate traffic. The bonus pushes the clearer cases
        # into the High band without overreliance on blacklist / brand
        # matching. Magnitude tunable via phishing.social_engineering.
        if not suppress_bonuses and len(urls) > 0 and s_social >= float(_REASON_T["social"]):
            raw += _URL_COMBO_BONUS

        # Additional bonus: phishing-prone TLDs (.xyz / .top / .click / ...
        # and RFC 2606 placeholders like .test / .example). Cardano protocols
        # don't live in these TLDs; a URL on one of them paired with Tier-2
        # text is very high-signal, so this bonus stacks on top of the URL
        # combo. Magnitude tunable via phishing.social_engineering.
        if (
            not suppress_bonuses
            and s_social >= float(_REASON_T["social"])
            and any(_has_phishing_prone_tld(u) for u in urls)
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
        if asset_name_urls:
            reasons.append("url_in_asset_name")

        # Severity classification (Polimi Section 4.9.3)
        severity = None
        if s_blacklist == 1.0:
            severity = "KNOWN_BAD"
        elif content_score >= _CRITICAL_T:
            severity = "SUSPICIOUS_NEW_DOMAIN"
        else:
            severity = "SOCIAL_ENGINEERING"

        blacklist_patterns = external.get_phishing_patterns()
        metadata_labels = (
            sorted({str(k) for k in (metadata or {}).keys()}) if isinstance(metadata, dict) else []
        )

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
            url_records.append(
                {
                    "url": url,
                    "severity": url_severity,
                    "phishing_tld": phishing_tld,
                }
            )

        evidence = {
            "severity": severity,
            "se_tier": se_tier,
            "urls": url_records,
            "url_count": len(urls),
            "asset_name_urls": asset_name_urls,
            "recipient_count": recipient_count,
            "metadata_labels": metadata_labels,
        }
        if asset_name_urls:
            evidence["url_token_delivery"] = {
                "recipient_pairs_total": url_pairs_total,
                "recipient_pairs_net_new": url_pairs_net_new,
                "bonuses_suppressed": suppress_bonuses,
            }

        return ScorerResult(
            score=final_score,
            sub_scores=sub_scores,
            reasons=reasons,
            baseline_source=bl_source,
            severity=severity,
            evidence=evidence,
        )

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _extract_urls(self, features: dict[str, Any]) -> list[str]:
        """Extract URL candidates from every known carrier and validate that
        each one has a real public-suffix TLD.

        Three carriers are scanned:

          1. Tx-level metadata under a relevant label (``674``, ``721``).
          2. Inline datums on outputs — walks the Plutus-Data tree and
             collects any UTF-8 decodable span. Catches CIP-68
             reference-NFT phishing (metadata lives in the datum, not in
             auxiliary data).
          3. Decoded native-asset names from the mint map and output value
             bundles. Catches the URL-named scam-token airdrop, which
             carries no metadata or datum at all.

        Bare-domain forms (``cardano-drop.io/claim``) are matched alongside
        scheme-prefixed URLs in every carrier, then filtered through the PSL
        snapshot so bare-word matches like ``3.14`` don't survive.
        """
        candidates = list(self._metadata_datum_url_candidates(features))

        # --- Carrier 3: decoded native-asset names -----------------------
        if _ASSET_CARRIER_ENABLED:
            candidates.extend(self._asset_name_candidates(features))

        return _validate_candidates(candidates)

    def _metadata_datum_url_candidates(self, features: dict[str, Any]) -> list[str]:
        """Un-validated URL candidates from carriers 1 and 2 only (tx-level
        metadata labels + inline datums), excluding asset names.

        Split out so the self-change bonus gate can ask "did a
        metadata/datum carrier produce a URL?" directly, instead of
        string-differencing the full URL set against the asset-name set: a
        URL that happens to appear in BOTH a metadata message and a
        self-change asset name must still count as metadata-delivered (and
        so keep its bonuses), which the string-difference could not tell
        apart.
        """
        candidates: list[str] = []

        # --- Carrier 1: tx-level metadata labels ------------------------
        metadata = features.get("metadata") or {}
        if isinstance(metadata, dict):
            for label_key in _RELEVANT_LABELS:
                content = metadata.get(label_key) or metadata.get(int(label_key))
                if content is None:
                    continue
                text = self._flatten_to_text(content)
                candidates.extend(_url_candidates(text))
                # A URL delivered as a CBOR bytes metadatum is hex, not text,
                # so _flatten_to_text returns the hex and misses it. Decode
                # bytes/CBOR-shaped values the same way inline datums are
                # handled and scan the recovered spans.
                for span in _decode_datum_strings(content):
                    candidates.extend(_url_candidates(span))

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
                    candidates.extend(_url_candidates(decoded))

        return candidates

    def _asset_name_candidates(self, features: dict[str, Any]) -> list[str]:
        """Raw URL/domain regex hits inside decoded asset names (un-validated)."""
        hits: list[str] = []
        for name in _decode_asset_name_strings(features.get("raw_data") or {}):
            hits.extend(_url_candidates(name))
        return hits

    def _flatten_to_text(self, obj: Any, depth: int = 0) -> str:
        """Recursively flatten a metadata value to a single string.

        CIP-20 stores long values as arrays of <=64-byte text chunks that the
        spec defines as concatenated without separators (the chunking is a
        CBOR-encoding workaround, not a content boundary). When a list is
        purely strings, join with ``""`` so URLs split across chunks
        reconstitute correctly; otherwise fall back to space-joining so
        nested structures still render readably for the SE-tier regex pass.

        ``depth`` bounds the descent (see _MAX_METADATA_FLATTEN_DEPTH) so an
        adversarially deep metadata value cannot raise RecursionError.
        """
        if depth > _MAX_METADATA_FLATTEN_DEPTH:
            logger.debug("metadata flatten hit depth cap %d", _MAX_METADATA_FLATTEN_DEPTH)
            return ""
        if isinstance(obj, str):
            return obj
        if isinstance(obj, list):
            if all(isinstance(item, str) for item in obj):
                return "".join(obj)
            return " ".join(self._flatten_to_text(item, depth + 1) for item in obj)
        if isinstance(obj, dict):
            parts = []
            for v in obj.values():
                parts.append(self._flatten_to_text(v, depth + 1))
            return " ".join(parts)
        return str(obj) if obj is not None else ""

    def _score_blacklist(self, urls: list[str]) -> float:
        """Score URLs against phishing domain patterns."""
        patterns = external.get_phishing_patterns()
        max_score = 0.0
        for url in urls:
            for pat in patterns:
                if pat.search(url):
                    max_score = max(max_score, 1.0)
        return max_score

    def _score_domain_suspicion(self, urls: list[str]) -> float:
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
        legit_info = [(_registrable_domain(d), _brand(d)) for d in known_domains]
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
                sim = fuzz.ratio(tx_brand_lc, legit_brand.lower()) / FUZZ_RATIO_SCALE
                if float(_SIM_RANGE["lo"]) < sim < float(_SIM_RANGE["hi"]):
                    max_brand_sim = max(max_brand_sim, sim)

        p50_b, p99_b = _anchor(_FIXED, "brand_sim")
        s_brand = normalise(max_brand_sim, p50=p50_b, p99=p99_b)

        # Domain age: requires WHOIS API (deferred to mainnet).
        # When domain age is available, composite = 0.50 * s_age + 0.50 * s_brand.
        # Until then, use brand similarity as the sole domain suspicion signal.
        return s_brand

    def _score_social_engineering(self, features: dict[str, Any]) -> float:
        """Score tx-level metadata AND inline-datum text for social
        engineering patterns. Thin wrapper that discards the tier label;
        the actual classification lives in ``_classify_social_engineering``
        so the evidence path can read it without recomputing.
        """
        score, _tier = self._classify_social_engineering(features)
        return score

    def _classify_social_engineering(
        self,
        features: dict[str, Any],
    ) -> tuple[float, str]:
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
        # Also decode bytes/CBOR-shaped metadata values (same reason as the URL
        # carrier: a bytes metadatum is hex, not text) so SE phrasing hidden in
        # a bytes metadatum is scanned too.
        if isinstance(metadata, dict):
            for label_key in _RELEVANT_LABELS:
                content = metadata.get(label_key) or metadata.get(int(label_key))
                if content is None:
                    continue
                for span in _decode_datum_strings(content):
                    text += " " + span.lower()

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

        # Asset names are a social-engineering carrier too: scam tokens are
        # routinely named with urgency/brand bait ("ClaimADARewards").
        if _ASSET_CARRIER_ENABLED:
            for name in _decode_asset_name_strings(raw_data):
                text += " " + name.lower()

        if not text:
            return 0.0, "None"

        # Tier 1 short-circuits the rest: a credential-request match is
        # near-deterministic so we don't bother counting urgency or brand.
        for pattern in external.TIER1_CREDENTIAL_PATTERNS:
            if pattern.lower() in text:
                return 1.0, "Tier 1: Credential harvesting"

        score = 0.0
        tiers_hit: list[str] = []

        urgency_matches = sum(
            1
            for pattern in external.TIER2_URGENCY_PATTERNS
            if re.search(pattern, text, re.IGNORECASE)
        )
        if urgency_matches > 0:
            score += min(
                float(_SE["urgency_cap"]),
                urgency_matches * float(_SE["urgency_increment"]),
            )
            tiers_hit.append("Tier 2: Urgency language")

        brand_matches = sum(1 for brand in external.TIER3_BRAND_NAMES if brand.lower() in text)
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
