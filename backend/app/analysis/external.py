"""External data sources for the detection system.

Provides curated reference data used by multiple scorers:
- Known legitimate Cardano protocol domains (brand similarity in phishing)
- Phishing URL patterns (blacklist matching)
- Legitimate token registry (fake token detection)

Data is cached in-memory with a configurable TTL.  In production, feeds
are refreshed daily by the baseline maintenance task.  For Preprod / first
deployment, static seed lists are used.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory caches with TTL
# ---------------------------------------------------------------------------

_cache: dict[str, dict] = {}
_CACHE_TTL_SECONDS = 86_400  # 24 hours


def _get_cached(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < _CACHE_TTL_SECONDS:
        return entry["data"]
    return None


def _set_cached(key: str, data: Any):
    _cache[key] = {"data": data, "ts": time.time()}


# ---------------------------------------------------------------------------
# Sender allowlist (known legitimate metadata senders)
# ---------------------------------------------------------------------------

# Addresses known to send legitimate metadata-bearing transactions.
# Prefix matching: if a tx input address starts with any of these, the
# phishing scorer gate returns False.  In production, this list would be
# fetched from an external curated registry.
_SENDER_ALLOWLIST_PREFIXES: list[str] = [
    # Cardano Foundation delegation portfolio
    "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer",
    # IOG/IOHK known distribution addresses
    "addr1q9d66zzs27kppmx8qc8h43q7m4hkxp5d39377lvjwwrm",
    # SundaeSwap protocol
    "addr1w9qzpelu9hn45pefc0xr4ac4kdxeswq7pndul2vuj59u8tq",
    # Minswap protocol
    "addr1z8snz7c4974vzdpxu65ruphl3zjdvtxw8strf2c2tmqnxz",
    # JPG Store marketplace
    "addr1w999n67e86jn6xal07pzxtrmqynspgx0fwmcmpua4wc6yq",
    # DripDropz distribution
    "addr1qxgkrphel4x4nsa95t0q03mfwfce7mxajkgkf2nkh46w",
]


def get_sender_allowlist() -> list[str]:
    """Return the list of allowed sender address prefixes."""
    cached = _get_cached("sender_allowlist")
    if cached is not None:
        return cached
    _set_cached("sender_allowlist", _SENDER_ALLOWLIST_PREFIXES)
    return _SENDER_ALLOWLIST_PREFIXES


def is_sender_allowlisted(addresses: list[str]) -> bool:
    """Check if any address in the list matches an allowlisted sender prefix."""
    prefixes = get_sender_allowlist()
    for addr in addresses:
        for prefix in prefixes:
            if addr.startswith(prefix):
                return True
    return False


# ---------------------------------------------------------------------------
# Protocol domain registry (phishing + brand similarity)
# ---------------------------------------------------------------------------

# Seed list of known legitimate Cardano ecosystem domains.
# Used for brand_similarity scoring in the Phishing and Fake Token scorers.
_KNOWN_PROTOCOL_DOMAINS: list[str] = [
    "cardano.org",
    "iohk.io",
    "emurgo.io",
    "sundaeswap.finance",
    "minswap.org",
    "app.minswap.org",
    "jpgstore.io",
    "jpg.store",
    "dexhunter.io",
    "wingriders.com",
    "muesliswap.com",
    "liqwid.finance",
    "indigo-protocol.com",
    "lenfi.io",
    "optim.finance",
    "genius-yield.co",
    "cardanoscan.io",
    "cexplorer.io",
    "pool.pm",
    "adastat.net",
    "dripdropz.io",
    "tosidrop.io",
    "book.io",
    "nmkr.io",
    "nftmaker.io",
    "spacebudz.io",
    "claymates.art",
    "adapulse.io",
    "cardanofeed.com",
]


def get_protocol_domains() -> list[str]:
    """Return the list of known legitimate protocol domains."""
    cached = _get_cached("protocol_domains")
    if cached is not None:
        return cached
    # In production this would be fetched from an external source.
    _set_cached("protocol_domains", _KNOWN_PROTOCOL_DOMAINS)
    return _KNOWN_PROTOCOL_DOMAINS


# ---------------------------------------------------------------------------
# Phishing URL blacklist (seed patterns)
# ---------------------------------------------------------------------------

# Seed patterns for known phishing indicators in URLs.
# Matched against full URL strings via regex (case-insensitive).
# In production, these would be supplemented by OpenPhish / PhishTank feeds.
# Source: manual curation from observed Cardano phishing campaigns (2024-2026).
# Categories:
#   Airdrop/claim bait: lure victims with free ADA promises
#   Governance bait: exploit CIP-1694/Voltaire governance participation
#   Wallet/credential harvest: direct credential theft attempts
_PHISHING_DOMAIN_PATTERNS: list[str] = [
    # Airdrop / claim bait
    r"cardano-airdrop",
    r"ada-claim",
    r"cardano-reward",
    r"free-ada",
    r"claim-ada",
    r"cardano-giveaway",
    r"ada-bonus",
    # Governance bait (CIP-1694 / Voltaire era)
    r"cardano-governance",
    r"cardano-vote",
    r"ada-governance",
    r"governance-reward",
    r"vote-to-earn",
    r"drep-reward",
    r"delegation-reward",
    # Wallet / credential harvest
    r"cardano-wallet-verify",
    r"seed-phrase",
    r"connect-wallet",
    r"wallet-sync",
    r"wallet-verify",
    r"ada-wallet-connect",
]

_compiled_patterns: list[re.Pattern] | None = None
_compiled_tier2: list[re.Pattern] | None = None


def get_phishing_patterns() -> list[re.Pattern]:
    """Return compiled regex patterns for phishing domain detection."""
    global _compiled_patterns
    if _compiled_patterns is not None:
        return _compiled_patterns
    _compiled_patterns = [re.compile(p, re.IGNORECASE) for p in _PHISHING_DOMAIN_PATTERNS]
    return _compiled_patterns


# ---------------------------------------------------------------------------
# Social engineering keyword tiers
# ---------------------------------------------------------------------------

# Tier 1: explicit credential requests (near-deterministic phishing indicator)
TIER1_CREDENTIAL_PATTERNS: list[str] = [
    "seed phrase",
    "recovery phrase",
    "private key",
    "mnemonic",
    "enter your seed",
    "enter your key",
    "wallet passphrase",
    "spending password",
]

# Tier 2: urgency, scarcity, and reward-bait language
# Source: manual curation from observed Cardano on-chain phishing metadata.
# Includes Voltaire-era governance bait patterns (2024-2026).
TIER2_URGENCY_PATTERNS: list[str] = [
    # Urgency / scarcity
    "limited time",
    "claim before",
    "expires",
    "act now",
    "only .{0,10} remaining",
    "hurry",
    "last chance",
    "don't miss",
    "ending soon",
    "immediate action",
    # Explicit urgency framing (common phishing openers)
    r"\burgent[:\s]",  # "URGENT:", "urgent ..."
    r"\btime[- ]sensitive\b",
    "verify your wallet",
    "verify wallet",
    "wallet needs",
    "resync.{0,20} wallet",
    "wallet.{0,20} resync",
    # Reward bait
    "earn rewards",
    "claim rewards",
    "claim your",
    "eligible for",
    "receive .{0,20} reward",
    "reward available",
    "bonus ada",
    "free ada",
    # Impersonation "you won" / prize-bait language
    r"\bcongratulations\b",
    r"you[' ]?(?:re| are|ve| have| ?ve)? ?(?:a )?winner\b",
    r"you[' ]?(?:ve| have)? ?won\b",
    "you.{0,10} selected",
    # Governance bait (CIP-1694 / Voltaire era)
    "vote .{0,30} to earn",
    "vote .{0,30} to receive",
    "vote .{0,30} reward",
    "delegate .{0,30} to earn",
    "governance reward",
    "governance grant",
    "treasury grant",
    "drep reward",
    "staking bonus",
]

# Tier 3: impersonation strings (brand names used in suspicious contexts)
TIER3_BRAND_NAMES: list[str] = [
    "cardano foundation",
    "iohk",
    "emurgo",
    "charles hoskinson",
    "sundaeswap",
    "minswap",
    "liqwid",
    "jpg store",
    "daedalus",
    "yoroi",
    "nami",
    "eternl",
    "flint",
    "typhon",
    "vespr",
]


# ---------------------------------------------------------------------------
# Legitimate token registry (fake token detection)
# ---------------------------------------------------------------------------

_CARDANO_TOKEN_REGISTRY_URL = "https://tokens.cardano.org/metadata"
_REGISTRY_FETCH_TIMEOUT = 10  # seconds per request

# Seed registry: used as fallback when the remote registry is unreachable.
# Maps token_name -> list of known legitimate policy_ids.
# Source: Cardano Token Registry (https://github.com/cardano-foundation/cardano-token-registry).
# Curated 2026-03-23 from top Cardano native tokens by ecosystem prominence.
# On startup, full metadata (ticker + display name) is fetched from the
# registry API at https://tokens.cardano.org/metadata/{subject}.
_SEED_TOKENS: dict[str, list[str]] = {
    "HOSKY": ["a0028f350aaabe0545fdcb56b039bfb08e4bb4d8c4d7c3c7d481c235"],
    "SNEK": ["279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f"],
    "MIN": ["29d222ce763455e3d7a09a665ce554f00ac89d2e99a1a83d267170c6"],
    "SUNDAE": ["9a9693a9a37912a5097918f97918d15240c92ab729a0b7c4aa144d77"],
    "WMT": ["1d7f33bd23d85e1a25d87d86fac4f199c3197a2f7afeb662a0f34e1e"],
    "INDY": ["533bb94a8850ee3ccbe483106489399112b74c905342cb1f14f5fc67"],
    "LENFI": ["8fef2d34078659493ce161a6c7fba4b56afefa8535296a5743f69587"],
    "iUSD": ["f66d78b4a3cb3d37afa0ec36461e51ecbde00f26c8f0a68f94b69880"],
    "DJED": ["8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"],
    "SHEN": ["8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"],
    "MILK": ["8a1cfae21368b8bebbbed9800fec304e95cce39a2a57dc35e2e3ebaa"],
    "AGIX": ["f43a62fdc3965df486de8a0d32fe800963589c41b38946602a8dc8e0"],
    "NTX": ["edfd7a1d77bcb8b884c474bdc92a16002d1571571ea33c4e1a4e6e36"],
    "OPTIM": ["e52964af4aeba6785d6ad4f81a5e48cff94fdddf5e5b5e9e04818c43"],
    "LQ": ["da8c30857834c6ae7203935b89278c532b3995245295456f993e1d24"],
    "JPG": ["682fe60c9918842b3323c43b5144bc3d52a23bd2fb81345560d73f63"],
}

# Well-known tokens to fetch from the Cardano Token Registry on startup.
# Each entry: (policy_id, hex_asset_name).
# The subject for the API is policy_id + hex_asset_name.
# Source: https://github.com/cardano-foundation/cardano-token-registry/tree/master/mappings
# Curated 2026-03-23 from top Cardano native tokens by ecosystem prominence.
# To add a token: find its mapping file in the registry, extract the policy_id
# (first 56 hex chars of the subject) and hex_asset_name (remaining chars).
_WELL_KNOWN_SUBJECTS: list[tuple] = [
    ("a0028f350aaabe0545fdcb56b039bfb08e4bb4d8c4d7c3c7d481c235", "484f534b59"),  # HOSKY
    ("279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f", "534e454b"),  # SNEK
    ("29d222ce763455e3d7a09a665ce554f00ac89d2e99a1a83d267170c6", "4d494e"),  # MIN
    ("9a9693a9a37912a5097918f97918d15240c92ab729a0b7c4aa144d77", "53554e444145"),  # SUNDAE
    ("1d7f33bd23d85e1a25d87d86fac4f199c3197a2f7afeb662a0f34e1e", "574d54"),  # WMT
    ("533bb94a8850ee3ccbe483106489399112b74c905342cb1f14f5fc67", "494e4459"),  # INDY
    ("8fef2d34078659493ce161a6c7fba4b56afefa8535296a5743f69587", "4c454e4649"),  # LENFI
    ("f66d78b4a3cb3d37afa0ec36461e51ecbde00f26c8f0a68f94b69880", "69555344"),  # iUSD
    ("8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61", "444a4544"),  # DJED
    ("8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61", "5348454e"),  # SHEN
    ("8a1cfae21368b8bebbbed9800fec304e95cce39a2a57dc35e2e3ebaa", "4d494c4b"),  # MILK
    ("f43a62fdc3965df486de8a0d32fe800963589c41b38946602a8dc8e0", "41474958"),  # AGIX
    ("edfd7a1d77bcb8b884c474bdc92a16002d1571571ea33c4e1a4e6e36", "4e5458"),  # NTX
    ("e52964af4aeba6785d6ad4f81a5e48cff94fdddf5e5b5e9e04818c43", "4f5054494d"),  # OPTIM
    ("da8c30857834c6ae7203935b89278c532b3995245295456f993e1d24", "4c51"),  # LQ
    ("682fe60c9918842b3323c43b5144bc3d52a23bd2fb81345560d73f63", "4a5047"),  # JPG
    (
        "b6a7467ea1deb012808ef4e87b5ff371e85f7142d7b356a40d9b42a0",
        "436f726e75636f70696173",
    ),  # Cornucopias
    ("5dac8536653edc12f6f5e1045d8164b9f59998d3bdc300fc928434894e4d4b52", ""),  # NMKR
    ("804f5544c1962a40546827cab750a88404dc7108c0f588b72964754f", "4d454c44"),  # MELD
    ("6ac8ef33b510ec004fe11585f7c5a9f0c07f0c23428ab4f29c1d7d10", "4d45534836"),  # MESH
    ("078eafce5cd7edafdf63900571f4c422da22b230e5048c55614d3b8b", "43484152"),  # CHAR
    ("884892bcdc360bcef87d6b3f806e7f9cd5ac30d999d49970e7a903ae", "5749474754"),  # WIGT
    ("25f0fc240e91bd95dcdaebd2ba7713fc5168ac77234a3d79449fc20c", "534f4349455459"),  # SOCIETY
    ("b34b3ea80060ace9427bda98690a73d33840e27aaa8d6edb7f0c757a", "634e455441"),  # cNETA
    ("af2e27f580f7f08e93190a81f72462f153026d06450924726645891b", "44524950"),  # DRIP
    (
        "d3501d9531fcc25e3ca4b6429318c2cc374c6e81c2779b7781dfea66",
        "4141444154",
    ),  # AADAT (AADA Finance)
    (
        "b6a7467ea1deb012808ef4e87b5ff371e85f7142d7b356a40d9b42a0",
        "57696e67526964657273",
    ),  # WingRiders
    ("6787a47e9f73efe4002d763337140da27afa8eb9a39413d2c39d4286", "5759464923"),  # WYFI
    ("c0ee29a85b13209423b10447d3c2e6a50641a15c57770e27cb9d5073", "57574f524c44"),  # WWORLD
    ("e4214b7cce62ac6fbba385d164df48e157eae5863521b4b67ca71d86", "4f4144415f4e4654"),  # Jpg.store
]


def _fetch_token_from_registry(subject: str) -> dict[str, Any] | None:
    """Fetch a single token entry from the Cardano Token Registry API."""
    url = f"{_CARDANO_TOKEN_REGISTRY_URL}/{subject}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_REGISTRY_FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
        logger.debug(f"Token registry fetch failed for {subject[:32]}...: {e}")
        return None


def _refresh_legitimate_tokens() -> tuple[dict[str, list[str]], int]:
    """Fetch well-known tokens from the Cardano Token Registry.

    Merges remote results with the seed list; returns ``(registry, fetched)``
    so the caller can tell a real refresh from a total outage (seeds-only)
    and avoid shrinking a previously complete cache.
    """
    registry: dict[str, list[str]] = {}

    # Start with seed tokens
    for name, policies in _SEED_TOKENS.items():
        registry[name] = list(policies)

    fetched = 0
    for policy_id, hex_asset_name in _WELL_KNOWN_SUBJECTS:
        subject = policy_id + hex_asset_name
        entry = _fetch_token_from_registry(subject)
        if not entry:
            continue
        fetched += 1

        # Extract ticker and name from the registry entry
        ticker_obj = entry.get("ticker")
        name_obj = entry.get("name")
        # Distinct names from the seed-loop's `name` above (which stays a str
        # in scope); these hold the registry entry's raw ticker/name value.
        ticker_val = ticker_obj.get("value") if isinstance(ticker_obj, dict) else ticker_obj
        name_val = name_obj.get("value") if isinstance(name_obj, dict) else name_obj

        for token_name in (ticker_val, name_val):
            if token_name and isinstance(token_name, str):
                existing = registry.get(token_name, [])
                if policy_id not in existing:
                    existing.append(policy_id)
                registry[token_name] = existing

    if fetched > 0:
        logger.info(
            f"Token registry: fetched {fetched}/{len(_WELL_KNOWN_SUBJECTS)} entries, "
            f"{len(registry)} names registered"
        )
    else:
        logger.warning("Token registry: remote fetch returned 0 entries, using seed list only")

    return registry, fetched


def get_legitimate_tokens(network: str = "mainnet") -> dict[str, list[str]]:
    """Return the legitimate token registry {name: [policy_ids]}.

    On first call, fetches from the Cardano Token Registry and caches for 24h.
    Falls back to the seed list on network errors.

    The seed list and remote registry entries are mainnet-only. On preview /
    preprod the same token names (iUSD, DJED, HOSKY, ...) are routinely minted
    by developers testing integrations, so returning the mainnet registry
    there produces guaranteed false positives. By default we return an empty
    registry for non-mainnet networks, which disables the fake_token scorer
    via its gate (no candidates to match against).

    Override: setting ``FAKE_TOKEN_TESTNET_MODE=True`` (env var) forces the
    mainnet registry to be used on ALL networks. This is intended for
    verifying fake_token detection with the ``internal/attacks.py`` harness
    on preprod/preview. Must be disabled before production deploy.
    """
    if network != "mainnet" and not settings.FAKE_TOKEN_TESTNET_MODE:
        return {}
    # NEVER fetch inline: this is called from gate()/score() on the scoring
    # hot path, and the remote registry refresh is ~30 sequential blocking
    # HTTP requests (worst case minutes), which previously stalled an entire
    # analysis batch on every 24 h cache expiry. The background task
    # (tasks/analysis.py) refreshes on a cadence via refresh_token_registry;
    # here we serve whatever is cached, stale included, falling back to the
    # curated seed list before the first refresh completes.
    entry = _cache.get("legitimate_tokens")
    if entry is not None:
        if time.time() - entry["ts"] >= _CACHE_TTL_SECONDS:
            logger.debug("Token registry cache stale; serving it pending background refresh")
        return entry["data"]
    logger.warning(
        "Token registry not yet fetched; serving seed list only (degraded "
        "coverage until the background refresh completes)"
    )
    return dict(_SEED_TOKENS)


def refresh_token_registry() -> int:
    """Force-refresh the token registry cache. Returns the number of names registered.

    A refresh that produces a SMALLER registry than the cached one must not
    replace it, regardless of how many subjects were fetched. A total outage
    (0 fetched) and a partial outage (the registry serving only a few of the
    ~31 well-known subjects) both shrink the result toward the seed list,
    and replacing a complete cache with that shrunken merge would silently
    cut fake_token impersonation coverage for a full refresh interval.
    Recall-first reasoning: a legitimate registry shrink (token delisting)
    is rare, and keeping a stale superset only risks impersonation FALSE
    POSITIVES on lookalikes of delisted tokens, while accepting the shrink
    risks MISSED impersonations of every dropped name; the stale superset
    is the safe direction. A grown-or-equal registry always replaces; a
    cold start with no prior cache stores whatever was merged (seeds only,
    in the worst case).
    """
    registry, fetched = _refresh_legitimate_tokens()
    # Read the raw cache (stale included): a refresh usually runs BECAUSE
    # the TTL expired, so the TTL-checked accessor would return None for
    # exactly the cache this guard protects.
    prior_entry = _cache.get("legitimate_tokens")
    previous = prior_entry["data"] if prior_entry else None
    if previous and len(previous) > len(registry):
        logger.warning(
            "Token registry %s outage (%d/%d subjects fetched): keeping "
            "previous cache of %d names instead of shrinking to %d",
            "total" if fetched == 0 else "partial",
            fetched,
            len(_WELL_KNOWN_SUBJECTS),
            len(previous),
            len(registry),
        )
        return len(previous)
    logger.info(
        "Token registry refresh applied: %d names cached (%d/%d subjects fetched)",
        len(registry),
        fetched,
        len(_WELL_KNOWN_SUBJECTS),
    )
    _set_cached("legitimate_tokens", registry)
    return len(registry)
