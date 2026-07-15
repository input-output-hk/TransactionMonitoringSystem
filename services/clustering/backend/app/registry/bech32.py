"""Minimal bech32 decoder — just enough to pull a Cardano payment credential.

A Shelley address is bech32-encoded as ``header(1 byte) || payment(28 bytes)
[|| stake(28 bytes)]``. For a script address the payment part *is* the script
hash, which is exactly the key the contracts registry is indexed by. We only
need to decode + extract; we never encode, and Cardano addresses use plain
bech32 (not bech32m), so no checksum-variant handling is required.

Vendored from the BIP-0173 reference implementation (public domain), trimmed to
the decode path. Pure stdlib, no third-party dependency.

PAIRED COPY: backend/app/utils/bech32.py carries the same validating core for
the host detection scorers (the two packages deliberately cannot import each
other; keep the decode logic textually in sync when either side changes).
"""

from __future__ import annotations

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


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
    or checksum) rather than raising — callers treat that as "no match".
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
    return hrp, data[:-6]


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
    ``None`` so the registry lookup simply misses.
    """
    raw = _decode_address_bytes(address)
    if raw is None or len(raw) < 29:
        return None
    return bytes(raw[1:29]).hex()


def stake_credential_hex(address: str) -> str | None:
    """Return the 56-hex stake credential of a Cardano address, or ``None``.

    The stake credential identifies the *wallet* that controls an address — many
    payment addresses can share one stake key. Address byte 0 is the header whose
    high nibble is the type:

      * base addresses (types ``0x0`` to ``0x3``) carry payment + stake creds;
        the stake credential is bytes ``[29:57]``.
      * reward/stake addresses (``stake…``, types ``0xE``/``0xF``) are the stake
        credential itself, bytes ``[1:29]``.
      * enterprise (``0x6``/``0x7``), pointer (``0x4``/``0x5``) and Byron
        addresses have no resolvable stake credential → ``None``.
    """
    raw = _decode_address_bytes(address)
    if not raw:
        return None
    addr_type = raw[0] >> 4
    if addr_type <= 0x03 and len(raw) >= 57:
        return bytes(raw[29:57]).hex()
    if addr_type in (0x0E, 0x0F) and len(raw) >= 29:
        return bytes(raw[1:29]).hex()
    return None
