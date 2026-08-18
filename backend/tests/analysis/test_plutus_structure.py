"""Display decoding of Plutus-Data datums and redeemers.

Read-path tests: nothing here feeds a score, but the input is attacker-controlled
on-chain data, so the bounds and the malformed-input paths matter as much as the
happy path.
"""

import cbor2

from app.analysis.plutus_structure import decode_datum_structure
from app.analysis.plutus_text import MAX_WALK_DEPTH

# Generous budget for the shape tests; the budget itself is exercised separately.
NODES = 20_000

# Plutus constructor CBOR tags (Plutus core spec / CIP-68 datum encoding).
TAG_CONSTR_0 = 121
TAG_CONSTR_7 = 1280
TAG_CONSTR_GENERAL = 102


def _hex(value) -> str:
    return cbor2.dumps(value).hex()


class TestCborHexDatums:
    def test_constructor_with_bytes_and_int_fields(self):
        decoded = decode_datum_structure(
            _hex(cbor2.CBORTag(TAG_CONSTR_0, [b"jpg.store", 42])), NODES
        )
        assert decoded.encoding == "cbor_hex"
        assert decoded.error is None
        assert decoded.truncated is False
        assert decoded.root is not None
        assert decoded.root.kind == "constructor"
        assert decoded.root.constructor_index == 0
        first, second = decoded.root.children
        assert first.kind == "bytes"
        assert first.value == b"jpg.store".hex()
        # Explorers show the UTF-8 reading beside the hex: token names, labels
        # and URLs ride in byte strings.
        assert first.text == "jpg.store"
        assert second.kind == "int"
        assert second.value == "42"

    def test_extended_tag_range_maps_to_constructor_seven(self):
        decoded = decode_datum_structure(_hex(cbor2.CBORTag(TAG_CONSTR_7, [b"x"])), NODES)
        assert decoded.root is not None
        assert decoded.root.constructor_index == 7

    def test_general_form_tag_carries_its_index(self):
        payload = cbor2.CBORTag(TAG_CONSTR_GENERAL, [3, [b"a"]])
        decoded = decode_datum_structure(_hex(payload), NODES)
        assert decoded.root is not None
        assert decoded.root.constructor_index == 3
        assert [c.kind for c in decoded.root.children] == ["bytes"]

    def test_map_nested_in_a_constructor_is_decoded(self):
        # cbor2 represents a map inside a tag payload as its own frozendict type,
        # which is a Mapping but not a dict; a dict-only check rendered every
        # such map as an empty "unknown" node.
        payload = cbor2.CBORTag(TAG_CONSTR_0, [{b"k": 1}])
        decoded = decode_datum_structure(_hex(payload), NODES)
        assert decoded.root is not None
        inner = decoded.root.children[0]
        assert inner.kind == "map"
        entry = inner.children[0]
        assert entry.kind == "map_entry"
        key, value = entry.children
        assert key.text == "k"
        assert value.value == "1"

    def test_top_level_list(self):
        decoded = decode_datum_structure(_hex([1, 2]), NODES)
        assert decoded.root is not None
        assert decoded.root.kind == "list"
        assert [c.value for c in decoded.root.children] == ["1", "2"]

    def test_non_printable_bytes_have_no_text_rendering(self):
        # Control characters are rejected rather than stripped: a datum is
        # attacker-controlled and must not reach a display as if it were text.
        decoded = decode_datum_structure(_hex(b"\x00\x01\x02"), NODES)
        assert decoded.root is not None
        assert decoded.root.kind == "bytes"
        assert decoded.root.text is None


class TestPlutusJsonDatums:
    def test_constructor_with_fields(self):
        decoded = decode_datum_structure(
            {"constructor": 1, "fields": [{"bytes": "61"}, {"int": 9}]},
            NODES,
        )
        assert decoded.encoding == "plutus_json"
        assert decoded.root is not None
        assert decoded.root.constructor_index == 1
        first, second = decoded.root.children
        assert (first.kind, first.text) == ("bytes", "a")
        assert (second.kind, second.value) == ("int", "9")

    def test_map_entries(self):
        decoded = decode_datum_structure(
            {"map": [{"k": {"bytes": "61"}, "v": {"int": 7}}]},
            NODES,
        )
        assert decoded.root is not None
        assert decoded.root.kind == "map"
        key, value = decoded.root.children[0].children
        assert key.value == "61"
        assert value.value == "7"

    def test_malformed_bytes_hex_is_preserved_not_dropped(self):
        decoded = decode_datum_structure({"bytes": "zz"}, NODES)
        assert decoded.root is not None
        assert decoded.root.kind == "bytes"
        assert decoded.root.value == "zz"
        assert decoded.root.text is None

    def test_unrecognised_node_still_descends(self):
        # A wrapper shape from a future Ogmios version should render something
        # rather than silently collapsing to nothing.
        decoded = decode_datum_structure({"someWrapper": {"int": 5}}, NODES)
        assert decoded.root is not None
        assert decoded.root.kind == "unknown"
        assert decoded.root.children[0].value == "5"


class TestBounds:
    def test_depth_cap_marks_truncated(self):
        nested = 1
        for _ in range(MAX_WALK_DEPTH + 10):
            nested = [nested]
        decoded = decode_datum_structure(_hex(nested), NODES)
        assert decoded.truncated is True

        def deepest(node, depth=0):
            if not node.children:
                return depth
            return max(deepest(c, depth + 1) for c in node.children)

        assert deepest(decoded.root) <= MAX_WALK_DEPTH + 1

    def test_node_budget_marks_truncated(self):
        # A flat wide datum is shallow, so only the node budget bounds it.
        decoded = decode_datum_structure(_hex(list(range(50))), 5)
        assert decoded.truncated is True
        assert any(c.kind == "truncated" for c in decoded.root.children)

    def test_within_budget_is_not_marked_truncated(self):
        decoded = decode_datum_structure(_hex([1, 2, 3]), NODES)
        assert decoded.truncated is False
        assert all(c.kind != "truncated" for c in decoded.root.children)


class TestUndecodable:
    def test_none_datum_reports_no_encoding(self):
        decoded = decode_datum_structure(None, NODES)
        assert decoded.encoding == "none"
        assert decoded.root is None
        assert decoded.error is None

    def test_non_hex_string_reports_an_error(self):
        decoded = decode_datum_structure("zzzz", NODES)
        assert decoded.root is None
        # "undecodable" is information on a flagged tx, so it must not look empty.
        assert decoded.error == "datum is not valid hex"

    def test_invalid_cbor_reports_an_error(self):
        # Valid hex, truncated CBOR: a byte-string header promising more bytes.
        decoded = decode_datum_structure("58ff", NODES)
        assert decoded.root is None
        assert decoded.error == "datum is not valid CBOR"

    def test_unrecognised_representation_reports_an_error(self):
        decoded = decode_datum_structure(12345, NODES)
        assert decoded.root is None
        assert decoded.error == "unrecognised datum representation"
