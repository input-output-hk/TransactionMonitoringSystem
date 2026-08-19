"""Datum and redeemer surfacing on the transaction detail response.

The payloads are not in any column: they live in the raw Ogmios blob, so these
tests pin that the endpoint reads them from there, resolves a hash-only datum
from the witness preimage map (the same rule the scoring gates apply), and
reports an unrecoverable blob as unavailable rather than as an absence of script
data.
"""

import cbor2

from app.api.transactions import _extract_script_detail

TAG_CONSTR_0 = 121
DATUM_HASH = "b" * 64


def _inline_hex(value=b"jpg.store") -> str:
    return cbor2.dumps(cbor2.CBORTag(TAG_CONSTR_0, [value])).hex()


class TestInlineDatum:
    def test_inline_datum_is_reported_with_hex_size_and_structure(self):
        payload = _inline_hex()
        datums, _ = _extract_script_detail({"outputs": [{"address": "addr1", "datum": payload}]})
        assert len(datums) == 1
        datum = datums[0]
        assert datum.output_index == 0
        assert datum.hex == payload
        assert datum.size_bytes == len(bytes.fromhex(payload))
        assert datum.resolved_from_witness is False
        assert datum.structure is not None
        assert datum.structure.root.constructor_index == 0
        assert datum.structure.root.children[0].text == "jpg.store"

    def test_outputs_without_a_datum_are_omitted(self):
        datums, _ = _extract_script_detail(
            {"outputs": [{"address": "addr1"}, {"address": "addr2", "datum": _inline_hex()}]}
        )
        assert [d.output_index for d in datums] == [1]

    def test_output_index_tracks_position(self):
        datums, _ = _extract_script_detail(
            {
                "outputs": [
                    {"address": "a"},
                    {"address": "b", "datum": _inline_hex(b"one")},
                    {"address": "c", "datum": _inline_hex(b"two")},
                ]
            }
        )
        assert [d.output_index for d in datums] == [1, 2]


class TestHashOnlyDatum:
    def test_preimage_is_resolved_from_the_witness_map(self):
        # The scoring gates size a hash-delivered datum from the witness
        # preimage; the detail view has to read the same bytes or the two
        # disagree about what the transaction's datum is.
        payload = _inline_hex()
        datums, _ = _extract_script_detail(
            {
                "outputs": [{"address": "addr1", "datumHash": DATUM_HASH}],
                "datums": {DATUM_HASH: payload},
            }
        )
        assert datums[0].datum_hash == DATUM_HASH
        assert datums[0].resolved_from_witness is True
        assert datums[0].hex == payload

    def test_unresolvable_hash_is_still_reported(self):
        # The output carries a datum hash whose preimage is not in the witness
        # set: report the hash, not silence.
        datums, _ = _extract_script_detail(
            {"outputs": [{"address": "addr1", "datumHash": DATUM_HASH}]}
        )
        assert len(datums) == 1
        assert datums[0].datum_hash == DATUM_HASH
        assert datums[0].hex is None
        assert datums[0].resolved_from_witness is False


class TestMalformedDatum:
    def test_bad_hex_leaves_size_unmeasured_rather_than_wrong(self):
        # None means "not measurable"; a character count is not a byte count.
        datums, _ = _extract_script_detail({"outputs": [{"datum": "zzzz"}]})
        assert datums[0].size_bytes is None
        assert datums[0].structure.error == "datum is not valid hex"

    def test_non_dict_output_is_skipped_not_raised(self):
        datums, _ = _extract_script_detail({"outputs": ["nonsense", {"datum": _inline_hex()}]})
        assert [d.output_index for d in datums] == [1]

    def test_non_list_outputs_yields_no_datums(self):
        datums, _ = _extract_script_detail({"outputs": "nonsense"})
        assert datums == []


class TestRedeemers:
    def test_redeemer_is_decoded_with_purpose_index_and_budget(self):
        payload = _inline_hex(b"redeem")
        _, redeemers = _extract_script_detail(
            {
                "redeemers": [
                    {
                        "validator": {"purpose": "spend", "index": 1},
                        "redeemer": payload,
                        "executionUnits": {"memory": 500, "steps": 900},
                    }
                ]
            }
        )
        assert len(redeemers) == 1
        entry = redeemers[0]
        assert entry.purpose == "spend"
        assert entry.index == 1
        assert entry.hex == payload
        assert entry.memory_units == 500
        assert entry.cpu_units == 900
        # A redeemer is Plutus data too, so it decodes the same way a datum does.
        assert entry.structure.root.children[0].text == "redeem"

    def test_v5_keyed_shape_is_supported(self):
        _, redeemers = _extract_script_detail(
            {"redeemers": {"spend:0": {"redeemer": _inline_hex()}}}
        )
        assert [(r.purpose, r.index) for r in redeemers] == [("spend", 0)]

    def test_absent_payload_decodes_to_no_structure(self):
        _, redeemers = _extract_script_detail({"redeemers": [{"purpose": "mint"}]})
        assert redeemers[0].hex is None
        assert redeemers[0].structure.root is None
        assert redeemers[0].structure.encoding == "none"


class TestUnavailableRawData:
    def test_missing_raw_data_yields_empty_lists(self):
        # The caller reports this as script_data_available=False so the UI can
        # say "unknown" instead of implying the tx has no datum or redeemer.
        assert _extract_script_detail(None) == ([], [])

    def test_empty_raw_data_yields_empty_lists(self):
        assert _extract_script_detail({}) == ([], [])
