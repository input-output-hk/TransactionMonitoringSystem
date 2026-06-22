"""Contract identity: classify a target as an address or a minting policy.

Source-neutral and pure: shared by the API, the job worker and the CLI. The
on-chain metadata *fetch* lives with the data-source adapter
(``app.sources.host_ch``), behind the ``ChainSource`` protocol.
"""

from __future__ import annotations

import re

# A bare 56-hex string is a script/policy hash; an ``addr...`` value must be a
# bech32 string (lowercase alphanumerics only). The strict charset matters
# because the target is interpolated into provider URL paths — disallowing
# ``/``, ``.``, ``%`` etc. closes a path-traversal / unintended-endpoint surface.
_POLICY_RE = re.compile(r"^[0-9a-fA-F]{56}$")
_ADDRESS_RE = re.compile(r"^addr(_test)?1[0-9a-z]{8,120}$")


def classify_target(value: str) -> str:
    """Return ``"policy"`` for a 56-hex hash, ``"address"`` for an addr…, else error."""
    v = value.strip()
    if _POLICY_RE.match(v):
        return "policy"
    if _ADDRESS_RE.match(v):
        return "address"
    raise ValueError(
        f"Cannot classify target {value!r}: expected a bech32 addr… address or a 56-hex policy id."
    )


def normalize_target(value: str) -> str:
    """Canonical form of a target wherever it enters the API (body or path).

    Hex policy ids are case-insensitive identifiers → lowercased, so the same
    policy can't exist under two casings and a path lookup always matches the
    stored row. Bech32 addresses are already lowercase-only by validation and
    pass through. Unclassifiable values pass through too (a path lookup will
    404 naturally; the onboarding endpoint validates separately)."""
    v = value.strip()
    if _POLICY_RE.match(v):
        return v.lower()
    return v


_TX_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def normalize_tx_hash(value: str) -> str:
    """Canonical (lowercase) form of a tx hash wherever it enters the API.

    Stored rows come from the chain source in lowercase hex; an uppercase path
    param would otherwise write/read label rows under a hash that never joins
    against ``transactions`` (a silent no-op label). Non-64-hex values pass
    through — downstream lookups miss naturally."""
    v = value.strip()
    return v.lower() if _TX_HASH_RE.match(v) else v
