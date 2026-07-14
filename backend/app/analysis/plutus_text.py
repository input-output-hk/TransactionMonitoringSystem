"""Text-span extraction from Plutus-Data datums.

Split out of the phishing scorer: walking an inline datum (hex-encoded CBOR
or Ogmios' Plutus-Data-JSON shape) and collecting UTF-8 decodable spans is
generic raw-data tooling, not scoring logic. The phishing scorer feeds the
spans to its URL and social-engineering scans; any future datum-content
consumer can reuse this without importing a scorer.
"""

import logging
from collections.abc import Mapping
from typing import Any, List, Optional

from app.analysis.features import get_cbor2

logger = logging.getLogger(__name__)

# Maximum nesting depth walked in an inline datum / decoded CBOR tree. The
# datum is attacker-controlled on-chain data; an unbounded recursive walk lets
# a maliciously deep structure (e.g. thousands of nested {"list": [...]})
# raise RecursionError, which the engine's per-scorer try/except swallows,
# silently scoring the phishing class -1 (a recall-evasion primitive). Bounding
# the descent makes the walk always terminate and return the spans found so far
# instead of crashing. 64 is far deeper than any legitimate Plutus datum yet
# well under CPython's default recursion limit given the closures' own frames.
_MAX_WALK_DEPTH = 64


def decode_datum_strings(datum: Any, min_len: int) -> List[str]:
    """Walk a Plutus-Data inline datum and collect every UTF-8 decodable
    text span of at least ``min_len`` characters. Handles two
    representations produced by ingestion:

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

    def _emit_bytes(blob: bytes) -> None:
        """Try to UTF-8 decode and emit; fall back to a byte-scan of
        printable-ASCII runs when the blob isn't valid UTF-8."""
        try:
            decoded = blob.decode("utf-8")
        except UnicodeDecodeError:
            _scan_bytes_for_strings(blob)
            return
        if len(decoded) >= min_len:
            results.append(decoded)

    def _scan_bytes_for_strings(blob: bytes) -> None:
        """Last-resort printable-ASCII scan. Only used when a byte
        string isn't valid UTF-8 so cbor2 / direct decode can't surface
        it cleanly."""
        start: Optional[int] = None
        for i, b in enumerate(blob):
            if 0x20 <= b < 0x7F:
                if start is None:
                    start = i
            else:
                if start is not None and i - start >= min_len:
                    try:
                        results.append(blob[start:i].decode("utf-8"))
                    except UnicodeDecodeError:
                        pass
                start = None
        if start is not None and len(blob) - start >= min_len:
            try:
                results.append(blob[start:].decode("utf-8"))
            except UnicodeDecodeError:
                pass

    def _walk_cbor(node: Any, depth: int = 0) -> None:
        """Walk the cbor2-parsed structure. CBOR text strings come out
        as ``str``, byte strings as ``bytes``, maps as ``dict`` or (cbor2
        v6, for maps nested inside a tag) the hashable ``cbor2.frozendict``,
        arrays as ``list``/``tuple``, and Plutus-Data constructors as
        ``cbor2.CBORTag`` whose ``.value`` is the fields array. Match maps
        by ``Mapping`` so frozendict leaves are not silently skipped."""
        if depth > _MAX_WALK_DEPTH:
            logger.debug("datum CBOR walk hit depth cap %d", _MAX_WALK_DEPTH)
            return
        if isinstance(node, bytes):
            _emit_bytes(node)
            return
        if isinstance(node, str):
            if len(node) >= min_len:
                results.append(node)
            return
        if isinstance(node, Mapping):
            for k, v in node.items():
                _walk_cbor(k, depth + 1)
                _walk_cbor(v, depth + 1)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                _walk_cbor(item, depth + 1)
            return
        # cbor2.CBORTag has ``.value``; duck-type to avoid an import
        # dependency at this module's top.
        inner = getattr(node, "value", None)
        if inner is not None and not isinstance(node, (int, float, bool)):
            _walk_cbor(inner, depth + 1)

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

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > _MAX_WALK_DEPTH:
            logger.debug("datum walk hit depth cap %d", _MAX_WALK_DEPTH)
            return
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
            if len(node) >= min_len:
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
                    _walk(item, depth + 1)
                return
            if "map" in node and isinstance(node["map"], list):
                for entry in node["map"]:
                    if isinstance(entry, dict):
                        _walk(entry.get("k"), depth + 1)
                        _walk(entry.get("v"), depth + 1)
                return
            if "fields" in node and isinstance(node["fields"], list):
                for field in node["fields"]:
                    _walk(field, depth + 1)
                return
            # Fallback for generic dicts
            for k, v in node.items():
                _walk(k, depth + 1)
                _walk(v, depth + 1)
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)
            return

    _walk(datum)
    return results
