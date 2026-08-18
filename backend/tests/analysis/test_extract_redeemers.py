"""Structured redeemer extraction across both Ogmios payload shapes.

Ogmios v5 keys a dict by "<purpose>:<index>" and carries no purpose on the value;
v6 emits a list with the purpose under ``validator``. Reading the purpose from
the wrong place for the v5 shape once returned nothing for every entry, silently
disabling multiple_sat's uniform-sweep guard, so parity between the two shapes is
what these tests pin.
"""

from app.analysis.features import (
    REDEEMER_INDEX_UNKNOWN,
    REDEEMER_PURPOSE_SPEND,
    extract_redeemers,
)
from app.analysis.scorers.multiple_sat import _spend_redeemer_payloads

V5 = {
    "redeemers": {
        "spend:0": {"redeemer": "d87980", "executionUnits": {"memory": 5, "cpu": 7}},
        "mint:1": {"redeemer": "ff"},
    }
}

V6 = {
    "redeemers": [
        {
            "validator": {"purpose": "spend", "index": 0},
            "redeemer": "d87980",
            "executionUnits": {"memory": 5, "steps": 7},
        },
        {"validator": {"purpose": "mint", "index": 1}, "redeemer": "ff"},
    ]
}


class TestShapeParity:
    def test_both_shapes_yield_the_same_purposes_and_payloads(self):
        v5 = {(e.purpose, e.index, e.payload_hex) for e in extract_redeemers(V5)}
        v6 = {(e.purpose, e.index, e.payload_hex) for e in extract_redeemers(V6)}
        assert v5 == v6
        assert ("spend", 0, "d87980") in v5

    def test_exec_units_read_from_either_budget_key(self):
        # v5 says "cpu", v6 says "steps"; both are the CPU budget.
        for payload in (V5, V6):
            spend = next(
                e for e in extract_redeemers(payload) if e.purpose == REDEEMER_PURPOSE_SPEND
            )
            assert (spend.memory_units, spend.cpu_units) == (5, 7)


class TestIndexSentinel:
    def test_missing_index_is_the_sentinel(self):
        entries = extract_redeemers({"redeemers": [{"purpose": "mint", "redeemer": "ff"}]})
        assert entries[0].index == REDEEMER_INDEX_UNKNOWN

    def test_garbage_index_degrades_to_the_sentinel(self):
        entries = extract_redeemers(
            {"redeemers": [{"validator": {"purpose": "spend", "index": "x"}, "redeemer": "ff"}]}
        )
        assert entries[0].index == REDEEMER_INDEX_UNKNOWN

    def test_index_zero_is_distinguishable_from_unknown(self):
        entries = extract_redeemers(V6)
        assert entries[0].index == 0
        assert REDEEMER_INDEX_UNKNOWN != 0


class TestPayloadTriState:
    def test_absent_payload_is_none(self):
        entries = extract_redeemers({"redeemers": [{"purpose": "spend"}]})
        assert entries[0].payload_hex is None

    def test_empty_payload_is_kept_as_empty_string(self):
        # Present-but-empty and absent are not interchangeable: the uniform-sweep
        # guard compares payload identity across a script's inputs.
        entries = extract_redeemers({"redeemers": [{"purpose": "spend", "redeemer": ""}]})
        assert entries[0].payload_hex == ""


class TestMalformedInput:
    def test_no_redeemers_is_empty(self):
        assert extract_redeemers({}) == []
        assert extract_redeemers({"redeemers": None}) == []
        assert extract_redeemers({"redeemers": []}) == []

    def test_non_dict_entries_are_skipped_not_raised(self):
        # raw_data is untrusted chain data; a raise here would cost the page.
        assert extract_redeemers({"redeemers": ["x", 3]}) == []

    def test_unexpected_container_type_is_empty(self):
        assert extract_redeemers({"redeemers": "nonsense"}) == []


class TestScorerProjectionUnchanged:
    """multiple_sat now projects off extract_redeemers; its contract must hold."""

    def test_spend_payloads_only(self):
        for payload in (V5, V6):
            assert _spend_redeemer_payloads(payload) == ["d87980"]

    def test_absent_payload_is_dropped(self):
        assert _spend_redeemer_payloads({"redeemers": [{"purpose": "spend"}]}) == []

    def test_empty_payload_is_retained(self):
        # Matches the pre-refactor behaviour (isinstance(payload, str) was True
        # for ""), which the uniform-sweep guard's payload comparison relies on.
        assert _spend_redeemer_payloads({"redeemers": [{"purpose": "spend", "redeemer": ""}]}) == [
            ""
        ]

    def test_non_spend_purposes_excluded(self):
        assert _spend_redeemer_payloads({"redeemers": {"mint:0": {"redeemer": "ff"}}}) == []
