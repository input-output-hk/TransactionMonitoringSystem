"""Bech32 decoding for Cardano addresses: validating core + lenient wrapper.

PAIRED COPY: services/clustering/backend/app/registry/bech32.py carries the
same validating core (the two packages deliberately cannot import each other;
keep the decode logic textually in sync when either side changes).

Two entry points with different failure contracts:

- ``payment_credential_hex`` / ``stake_credential_hex``: strict BIP-0173
  decode with checksum validation; malformed input yields ``None``.
- ``payment_credential_or_raw``: the detection scorers' grouping key. It
  strips the checksum WITHOUT validating and falls back to the raw address on
  any structural failure, so a checksum-corrupted (attacker-mangled) address
  still groups with its well-formed siblings and a non-address string still
  yields a usable per-address key. Switching the scorers to the strict
  variant would change grouping keys for malformed addresses: that is a
  RECALL decision, gated by the contract tests in
  backend/tests/analysis/scorers/test_payment_credential.py, and is
  deliberately out of scope here.

Validating core vendored from the BIP-0173 reference implementation (public
domain), trimmed to the decode path. Pure stdlib.
"""

from __future__ import annotations

from functools import lru_cache

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
# BIP-0173: the trailing 6 data characters are the checksum.
_CHECKSUM_LEN = 6
# CIP-19: Blake2b-224 hash size, payment + stake creds.
PAYMENT_CRED_BYTES = 28
# Each tx triggers up to 3 * (N_inputs + N_outputs) grouping calls across the
# same address set (grouping + lovelace flow + asset flow), so the wrapper is
# cached; 4096 comfortably covers a block's worth of distinct addresses.
_CREDENTIAL_CACHE_SIZE = 4096


def _polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def bech32_decode(bech: str) -> tuple[str, list[int]] | None:
    """Decode a bech32 string to ``(hrp, data)`` (5-bit groups), or ``None``.

    Returns ``None`` for any malformed input (mixed case, bad charset, length,
    or checksum) rather than raising: callers treat that as "no match".
    """
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return None
    if bech.lower() != bech and bech.upper() != bech:
        return None
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 1023:
        return None
    if not all(x in _CHARSET for x in bech[pos + 1 :]):
        return None
    hrp = bech[:pos]
    data = [_CHARSET.find(x) for x in bech[pos + 1 :]]
    if _polymod(_hrp_expand(hrp) + data) != 1:
        return None
    return hrp, data[:-_CHECKSUM_LEN]


def convertbits(data: list[int], frombits: int, tobits: int, pad: bool = True) -> list[int] | None:
    """Regroup ``data`` from ``frombits``-wide to ``tobits``-wide values."""
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def _decode_address_bytes(address: str) -> list[int] | None:
    """Decode a bech32 Cardano address to its raw bytes, or ``None``."""
    decoded = bech32_decode(address.strip())
    if decoded is None:
        return None
    return convertbits(decoded[1], 5, 8, pad=False)


def payment_credential_hex(address: str) -> str | None:
    """Return the 56-hex payment credential of a Shelley ``addr…``, else ``None``.

    The payment credential is bytes ``[1:29]`` of the decoded address (the byte
    after the network/type header). Malformed or too-short addresses yield
    ``None``.
    """
    raw = _decode_address_bytes(address)
    if raw is None or len(raw) < 1 + PAYMENT_CRED_BYTES:
        return None
    return bytes(raw[1 : 1 + PAYMENT_CRED_BYTES]).hex()


def stake_credential_hex(address: str) -> str | None:
    """Return the 56-hex stake credential of a Cardano address, or ``None``.

    Address byte 0 is the header whose high nibble is the type: base addresses
    (types ``0x0`` to ``0x3``) carry payment + stake creds and the stake
    credential is bytes ``[29:57]``; reward/stake addresses (types
    ``0xE``/``0xF``) are the stake credential itself, bytes ``[1:29]``;
    enterprise, pointer, and Byron addresses have none.
    """
    raw = _decode_address_bytes(address)
    if not raw:
        return None
    addr_type = raw[0] >> 4
    if addr_type <= 0x03 and len(raw) >= 1 + 2 * PAYMENT_CRED_BYTES:
        return bytes(raw[1 + PAYMENT_CRED_BYTES : 1 + 2 * PAYMENT_CRED_BYTES]).hex()
    if addr_type in (0x0E, 0x0F) and len(raw) >= 1 + PAYMENT_CRED_BYTES:
        return bytes(raw[1 : 1 + PAYMENT_CRED_BYTES]).hex()
    return None


@lru_cache(maxsize=_CREDENTIAL_CACHE_SIZE)
def payment_credential_or_raw(addr: str) -> str:
    """Grouping key for the detection scorers: payment credential or raw input.

    Two addresses sharing the same payment credential (script hash) but
    differing in stake credential must group together: a validator
    vulnerability can be exploited by spending multiple UTxOs at the same
    script with distinct stake credentials, putting them at distinct
    ``address`` strings but the same script. Grouping by raw address misses
    the attack (canonical purchase-offer double-satisfaction shape).

    The 6-char bech32 checksum at the tail is stripped WITHOUT validation,
    and any structural failure returns the raw address: see the module
    docstring for why this leniency is load-bearing for recall.
    """
    if not addr or "1" not in addr:
        return addr
    try:
        data_part = addr.rsplit("1", 1)[1].lower()
        if len(data_part) <= _CHECKSUM_LEN:
            return addr
        # Strip the trailing checksum without validating it; callers must not
        # rely on a successful decode meaning the address is well-formed.
        data_part = data_part[:-_CHECKSUM_LEN]
        data: list[int] = []
        for c in data_part:
            v = _CHARSET.find(c)
            if v == -1:
                return addr
            data.append(v)
        # Layout: 1 header byte + 28-byte payment cred + (optional stake cred).
        if 5 * len(data) < 8 + PAYMENT_CRED_BYTES * 8:
            return addr
        raw = convertbits(data, 5, 8, pad=True)
        if raw is None:
            return addr
        return bytes(raw[1 : 1 + PAYMENT_CRED_BYTES]).hex()
    except Exception:
        return addr
