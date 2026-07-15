"""Tests for the offline contracts-registry label lookup.

Exercises bech32 address decoding, direct policy-hash matching, and the misses
that must degrade to an empty label. Uses the vendored snapshot (no network).
"""

from __future__ import annotations

from app.features.graph import entity_key
from app.registry import lookup_label, script_hash_for
from app.registry.bech32 import (
    _CHARSET,
    _hrp_expand,
    _polymod,
    convertbits,
    payment_credential_hex,
    stake_credential_hex,
)
from app.registry.loader import label_map


def _encode_address(raw: bytes, hrp: str = "addr") -> str:
    """Bech32-encode raw address bytes (round-trip helper for the decode tests)."""
    data = convertbits(list(raw), 8, 5, pad=True)
    assert data is not None
    chk_values = _hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]
    polymod = _polymod(chk_values) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


# Minswap "Order Contract" — present in the vendored snapshot.
MINSWAP_ORDER_HASH = "a65ca58a4e9c755fa830173d2a5caed458ac0c73f97db7faae2e7e3b"
MINSWAP_ORDER_LABEL = "Minswap Order Contract"
# Valid mainnet enterprise-script address whose payment credential is the hash
# above (header 0x71 || 28-byte script hash, bech32-encoded with hrp "addr").
MINSWAP_ORDER_ADDR = "addr1wxn9efv2f6w82hagxqtn62ju4m293tqvw0uhmdl64ch8uwc0h43gt"


def test_label_map_loads_vendored_snapshot() -> None:
    m = label_map()
    assert len(m) > 100  # ~641 contracts vendored
    assert m[MINSWAP_ORDER_HASH] == MINSWAP_ORDER_LABEL


def test_payment_credential_hex_extracts_script_hash() -> None:
    assert payment_credential_hex(MINSWAP_ORDER_ADDR) == MINSWAP_ORDER_HASH


def test_payment_credential_hex_returns_none_for_malformed() -> None:
    assert payment_credential_hex("addr1notvalid") is None
    assert payment_credential_hex("not-an-address") is None
    assert payment_credential_hex("") is None


def test_lookup_label_policy_direct_match() -> None:
    assert lookup_label(MINSWAP_ORDER_HASH, "policy") == MINSWAP_ORDER_LABEL
    # Case-insensitive on the incoming hash.
    assert lookup_label(MINSWAP_ORDER_HASH.upper(), "policy") == MINSWAP_ORDER_LABEL


def test_lookup_label_address_decodes_then_matches() -> None:
    assert lookup_label(MINSWAP_ORDER_ADDR, "address") == MINSWAP_ORDER_LABEL


def test_lookup_label_misses_return_empty_string() -> None:
    assert lookup_label("00" * 28, "policy") == ""  # unknown hash
    assert lookup_label("addr1notvalid", "address") == ""  # undecodable
    assert lookup_label(MINSWAP_ORDER_HASH, "unknown_type") == ""  # bad type


def test_script_hash_for_policy_address_and_garbage() -> None:
    assert script_hash_for(MINSWAP_ORDER_HASH, "policy") == MINSWAP_ORDER_HASH
    assert script_hash_for(MINSWAP_ORDER_HASH.upper(), "policy") == MINSWAP_ORDER_HASH
    assert script_hash_for(MINSWAP_ORDER_ADDR, "address") == MINSWAP_ORDER_HASH
    assert script_hash_for("addr1notvalid", "address") is None
    assert script_hash_for(MINSWAP_ORDER_HASH, "unknown_type") is None


def test_stake_credential_extraction_and_entity_grouping() -> None:
    # A mainnet base address has header 0x01 (type 0, network 1) + payment(28) +
    # stake(28). Two addresses with different payment creds but the SAME stake key
    # must resolve to the same entity (one wallet, many payment addresses).
    stake = bytes(range(28))
    addr1 = _encode_address(bytes([0x01]) + bytes([0xAA] * 28) + stake)
    addr2 = _encode_address(bytes([0x01]) + bytes([0xBB] * 28) + stake)
    assert stake_credential_hex(addr1) == stake.hex()
    assert stake_credential_hex(addr2) == stake.hex()
    assert entity_key(addr1) == entity_key(addr2) == f"stake:{stake.hex()}"


def test_stake_credential_none_for_enterprise_and_garbage() -> None:
    # MINSWAP_ORDER_ADDR is an enterprise-script address (header 0x71) — no stake
    # credential, so entity_key falls back to the raw address.
    assert stake_credential_hex(MINSWAP_ORDER_ADDR) is None
    assert stake_credential_hex("not-an-address") is None
    assert entity_key(MINSWAP_ORDER_ADDR) == MINSWAP_ORDER_ADDR


def test_local_overrides_are_applied() -> None:
    # Djed contracts are registered via data/overrides.json, not upstream.
    djed_v1 = "8952dc463eb173e8f71c78229fe071c3eca694968c71f61ee3491ebd"
    djed_v2 = "021a32ee5621d4e94df1d9bef6baa284a7cc75ba51bb5fb804c6c471"
    assert label_map()[djed_v1] == "Djed v1 Stablecoin Contract"
    assert lookup_label(djed_v2, "policy") == "Djed v2 Stablecoin Contract"
