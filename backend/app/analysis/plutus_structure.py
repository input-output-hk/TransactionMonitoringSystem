"""Display-ready structure of a Plutus-Data datum.

Every existing datum consumer in this codebase collapses the decoded tree to a
scalar: entropy bits, largest-leaf ratio, a boolean, or a list of text spans.
None of them can answer the question an analyst asks when they open a flagged
transaction, which is "what does this datum actually say". This module walks the
same two representations :mod:`app.analysis.plutus_text` handles and preserves
the shape instead of discarding it: constructor tags, field order, map entries,
and each leaf's type and value.

It is a READ-PATH module. Nothing here feeds a score, so it may render a datum
that the detection path deliberately treats as unassessable; the walk is still
bounded the same way, because the input is attacker-controlled on-chain data.
"""

import logging
from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from app.analysis.features import get_cbor2
from app.analysis.plutus_text import MAX_WALK_DEPTH

logger = logging.getLogger(__name__)

# Plutus-Data constructor tags, per the CBOR encoding in the Plutus core spec
# (also given in CIP-68's datum-encoding section): tags 121-127 carry
# constructors 0-6, tags 1280-1400 carry constructors 7-127, and tag 102 is the
# general form whose payload is [constructor_index, [fields]].
_TAG_CONSTR_COMPACT_BASE: Final = 121
_TAG_CONSTR_COMPACT_MAX: Final = 127
_TAG_CONSTR_EXTENDED_BASE: Final = 1280
_TAG_CONSTR_EXTENDED_MAX: Final = 1400
_TAG_CONSTR_GENERAL: Final = 102
# The general form's payload is exactly (constructor index, field list).
_GENERAL_CONSTR_ARITY: Final = 2

NodeKind = Literal[
    "constructor",
    "list",
    "map",
    "map_entry",
    "bytes",
    "int",
    "text",
    "truncated",
    "unknown",
]


class DatumNode(BaseModel):
    """One node of a decoded datum tree.

    ``kind`` drives rendering. ``truncated`` is emitted in place of a subtree
    that hit the depth or node budget: the UI must show it as elided rather than
    as an empty structure, because "we stopped looking" and "there is nothing
    here" are different claims to make about a flagged transaction.
    """

    kind: NodeKind
    # Not named `constructor`: that is Object.prototype.constructor in the
    # TypeScript client, so a Partial<DatumNode> resolves the field to Function
    # and any structural type check against it fails confusingly.
    constructor_index: int | None = Field(
        None,
        description="Constructor index, for kind='constructor' only",
    )
    value: str | None = Field(
        None,
        description=(
            "Scalar rendering of a leaf: hex for bytes, decimal for int, the "
            "string itself for text. None for structural nodes."
        ),
    )
    text: str | None = Field(
        None,
        description=(
            "UTF-8 interpretation of a bytes leaf when it decodes cleanly and "
            "is printable. Explorers show this next to the hex because token "
            "names, labels and URLs ride in byte strings."
        ),
    )
    children: list["DatumNode"] = Field(default_factory=list)


class DecodedDatum(BaseModel):
    """A datum's decoded structure plus what could not be decoded."""

    root: DatumNode | None = Field(
        None,
        description="Decoded tree root; None when the payload could not be decoded at all",
    )
    encoding: Literal["cbor_hex", "plutus_json", "none"] = Field(
        "none",
        description="Which representation the datum arrived in",
    )
    truncated: bool = Field(
        False,
        description="True when the depth or node budget elided part of the tree",
    )
    error: str | None = Field(
        None,
        description=(
            "Why decoding failed, for display. A datum that cannot be decoded is "
            "reported as such rather than as an empty one."
        ),
    )


def _printable_text(blob: bytes) -> str | None:
    """UTF-8 rendering of a byte leaf, or None when it is not display-worthy.

    Control characters are rejected, not stripped: a datum is attacker-controlled
    and a leaf containing escape sequences must not reach a terminal or be shown
    as if it were ordinary text.
    """
    try:
        decoded = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not decoded or not decoded.isprintable():
        return None
    return decoded


def _constructor_from_tag(tag_number: int) -> int | None:
    """Constructor index encoded by a Plutus CBOR tag, or None if not a Constr tag."""
    if _TAG_CONSTR_COMPACT_BASE <= tag_number <= _TAG_CONSTR_COMPACT_MAX:
        return tag_number - _TAG_CONSTR_COMPACT_BASE
    if _TAG_CONSTR_EXTENDED_BASE <= tag_number <= _TAG_CONSTR_EXTENDED_MAX:
        return (
            tag_number
            - _TAG_CONSTR_EXTENDED_BASE
            + (_TAG_CONSTR_COMPACT_MAX - _TAG_CONSTR_COMPACT_BASE + 1)
        )
    return None


class _Budget:
    """Node allowance for one decode, so a wide datum cannot exhaust memory.

    The depth cap alone bounds only nesting; a single flat list of a million
    entries is shallow. Both limits report through ``hit`` so the caller can
    flag the result as truncated rather than serving a silently partial tree.
    """

    def __init__(self, max_nodes: int) -> None:
        self.remaining = max_nodes
        self.hit = False

    def take(self) -> bool:
        if self.remaining <= 0:
            self.hit = True
            return False
        self.remaining -= 1
        return True


def _walk_cbor(node: Any, depth: int, budget: _Budget) -> DatumNode:
    """Convert a cbor2-decoded Plutus value into a DatumNode tree."""
    if depth > MAX_WALK_DEPTH or not budget.take():
        budget.hit = True
        return DatumNode(kind="truncated")

    cbor2 = get_cbor2()
    if isinstance(node, cbor2.CBORTag):
        # cbor2 hands back a TUPLE for a tag's array payload (a bare CBOR array
        # decodes as a list), so the sequence tests below accept both.
        constructor = _constructor_from_tag(node.tag)
        if constructor is not None:
            fields = node.value if isinstance(node.value, list | tuple) else [node.value]
            return DatumNode(
                kind="constructor",
                constructor_index=constructor,
                children=[_walk_cbor(f, depth + 1, budget) for f in fields],
            )
        if (
            node.tag == _TAG_CONSTR_GENERAL
            and isinstance(node.value, list | tuple)
            and len(node.value) == _GENERAL_CONSTR_ARITY
        ):
            index, fields = node.value
            field_list = fields if isinstance(fields, list | tuple) else [fields]
            return DatumNode(
                kind="constructor",
                constructor_index=index if isinstance(index, int) else None,
                children=[_walk_cbor(f, depth + 1, budget) for f in field_list],
            )
        # A tag outside the Plutus set: render its payload rather than dropping it.
        return DatumNode(kind="unknown", children=[_walk_cbor(node.value, depth + 1, budget)])
    if isinstance(node, bool):
        # Checked before int: bool is an int subclass, and rendering True as "1"
        # would misreport the datum.
        return DatumNode(kind="unknown", value=str(node))
    if isinstance(node, int):
        return DatumNode(kind="int", value=str(node))
    if isinstance(node, bytes):
        return DatumNode(kind="bytes", value=node.hex(), text=_printable_text(node))
    if isinstance(node, str):
        return DatumNode(kind="text", value=node)
    if isinstance(node, list | tuple):
        # tuple as well as list: see the CBORTag payload note above.
        return DatumNode(
            kind="list",
            children=[_walk_cbor(item, depth + 1, budget) for item in node],
        )
    if isinstance(node, Mapping):
        # Mapping, not dict: cbor2 represents a map nested inside a tag payload
        # as its own frozendict type, which is a Mapping but fails isinstance
        # against dict, so a dict check silently rendered every map inside a
        # constructor as an empty "unknown" node.
        entries: list[DatumNode] = []
        for key, value in node.items():
            entries.append(
                DatumNode(
                    kind="map_entry",
                    children=[
                        _walk_cbor(key, depth + 1, budget),
                        _walk_cbor(value, depth + 1, budget),
                    ],
                )
            )
        return DatumNode(kind="map", children=entries)
    if node is None:
        return DatumNode(kind="unknown")
    return DatumNode(kind="unknown", value=str(node))


def _walk_plutus_json(node: Any, depth: int, budget: _Budget) -> DatumNode:
    """Convert Ogmios' Plutus-Data-JSON shape into a DatumNode tree."""
    if depth > MAX_WALK_DEPTH or not budget.take():
        budget.hit = True
        return DatumNode(kind="truncated")

    if isinstance(node, list):
        return DatumNode(
            kind="list",
            children=[_walk_plutus_json(item, depth + 1, budget) for item in node],
        )
    if not isinstance(node, dict):
        if isinstance(node, bool):
            return DatumNode(kind="unknown", value=str(node))
        if isinstance(node, int):
            return DatumNode(kind="int", value=str(node))
        if isinstance(node, str):
            return DatumNode(kind="text", value=node)
        return DatumNode(kind="unknown")

    if "constructor" in node:
        fields = node.get("fields")
        field_list = fields if isinstance(fields, list) else []
        index = node.get("constructor")
        return DatumNode(
            kind="constructor",
            constructor_index=index if isinstance(index, int) else None,
            children=[_walk_plutus_json(f, depth + 1, budget) for f in field_list],
        )
    if isinstance(node.get("list"), list):
        return DatumNode(
            kind="list",
            children=[_walk_plutus_json(i, depth + 1, budget) for i in node["list"]],
        )
    if isinstance(node.get("map"), list):
        entries = []
        for entry in node["map"]:
            if not isinstance(entry, dict):
                continue
            entries.append(
                DatumNode(
                    kind="map_entry",
                    children=[
                        _walk_plutus_json(entry.get("k"), depth + 1, budget),
                        _walk_plutus_json(entry.get("v"), depth + 1, budget),
                    ],
                )
            )
        return DatumNode(kind="map", children=entries)
    if isinstance(node.get("bytes"), str):
        raw_hex = node["bytes"]
        try:
            blob = bytes.fromhex(raw_hex)
        except ValueError:
            return DatumNode(kind="bytes", value=raw_hex)
        return DatumNode(kind="bytes", value=blob.hex(), text=_printable_text(blob))
    for int_key in ("int", "integer"):
        if isinstance(node.get(int_key), int) and not isinstance(node.get(int_key), bool):
            return DatumNode(kind="int", value=str(node[int_key]))
    if isinstance(node.get("string"), str):
        return DatumNode(kind="text", value=node["string"])
    # Not a recognised Plutus-JSON node: descend into values so a wrapper shape
    # from a future Ogmios version still renders something useful.
    return DatumNode(
        kind="unknown",
        children=[_walk_plutus_json(v, depth + 1, budget) for v in node.values()],
    )


def decode_datum_structure(datum: Any, max_nodes: int) -> DecodedDatum:
    """Decode a datum into a display tree.

    ``datum`` is whatever :func:`app.analysis.features.assessable_datum` returned:
    a hex-encoded CBOR string, an Ogmios Plutus-Data-JSON object, or None.

    Never raises on malformed input. A datum that cannot be decoded comes back
    with ``root=None`` and ``error`` set, which the UI must distinguish from an
    empty datum: on a flagged transaction, "undecodable" is information.
    """
    if datum is None:
        return DecodedDatum(encoding="none")
    budget = _Budget(max_nodes)
    if isinstance(datum, str):
        try:
            blob = bytes.fromhex(datum)
        except ValueError:
            return DecodedDatum(encoding="cbor_hex", error="datum is not valid hex")
        try:
            decoded = get_cbor2().loads(blob)
        except Exception as e:  # cbor2 raises several unrelated types
            logger.debug("datum CBOR decode failed: %s", e)
            return DecodedDatum(encoding="cbor_hex", error="datum is not valid CBOR")
        root = _walk_cbor(decoded, 0, budget)
        return DecodedDatum(root=root, encoding="cbor_hex", truncated=budget.hit)
    if isinstance(datum, dict | list):
        root = _walk_plutus_json(datum, 0, budget)
        return DecodedDatum(root=root, encoding="plutus_json", truncated=budget.hit)
    return DecodedDatum(encoding="none", error="unrecognised datum representation")
