"""Normalized contract identity derived from a winning class's evidence.

Pure tests: contract_address_of reads a dict and returns a string, so no DB or
scorer machinery is involved.
"""

from app.analysis.contract_identity import (
    CONTRACT_EVIDENCE_KEYS,
    NO_CONTRACT,
    contract_address_of,
)
from app.analysis.engine import _CLASS_NAMES
from app.models.transaction import AttackClass

SCRIPT_ADDR = "addr_test1wq9j5x8h0datumscripttarget"

# Classes that name a script address under the shared evidence key.
_SCRIPT_ADDRESS_CLASSES = (
    AttackClass.TOKEN_DUST.value,
    AttackClass.LARGE_VALUE.value,
    AttackClass.LARGE_DATUM.value,
    AttackClass.MULTIPLE_SAT.value,
)

# Classes that describe a relationship between addresses rather than an
# interaction with one contract, so they have no grouping identity at all.
_NO_IDENTITY_CLASSES = (
    AttackClass.PHISHING.value,
    AttackClass.CIRCULAR.value,
    AttackClass.FRONT_RUNNING.value,
)


class TestScriptAddressClasses:
    def test_each_script_class_resolves_its_target(self):
        for attack_class in _SCRIPT_ADDRESS_CLASSES:
            evidence = {attack_class: {"target_script_address": SCRIPT_ADDR}}
            assert contract_address_of(attack_class, evidence) == SCRIPT_ADDR

    def test_reads_only_the_winning_class(self):
        # A losing class's target must not be attributed to the winner: the
        # alert is labelled with max_class, so grouping has to agree with it.
        evidence = {
            "large_datum": {"target_script_address": SCRIPT_ADDR},
            "token_dust": {"target_script_address": "addr_test1wOTHER"},
        }
        assert contract_address_of("large_datum", evidence) == SCRIPT_ADDR

    def test_whitespace_is_stripped(self):
        evidence = {"large_datum": {"target_script_address": f"  {SCRIPT_ADDR}  "}}
        assert contract_address_of("large_datum", evidence) == SCRIPT_ADDR


class TestContractAnomaly:
    def test_resolves_the_watched_target(self):
        evidence = {"contract_anomaly": {"target": SCRIPT_ADDR, "model_id": "m1"}}
        assert contract_address_of("contract_anomaly", evidence) == SCRIPT_ADDR


class TestNoIdentity:
    def test_classes_without_a_contract_return_the_sentinel(self):
        for attack_class in _NO_IDENTITY_CLASSES:
            assert contract_address_of(attack_class, {attack_class: {"x": 1}}) == NO_CONTRACT

    def test_sandwich_pool_id_is_not_treated_as_a_contract(self):
        # pool_id is a DEX pool identifier, not an address. Folding it in would
        # put two identifier namespaces in one column, so a consumer could not
        # tell whether to resolve a value as an address.
        evidence = {"sandwich": {"pool_id": "pool1abc"}}
        assert contract_address_of("sandwich", evidence) == NO_CONTRACT

    def test_fake_token_policy_id_is_not_treated_as_a_contract(self):
        evidence = {"fake_token": {"fake_policy_id": "a" * 56}}
        assert contract_address_of("fake_token", evidence) == NO_CONTRACT


class TestDegradedInput:
    def test_empty_max_class_is_the_sentinel(self):
        # max_class is "" when no class was applicable at all.
        assert contract_address_of("", {}) == NO_CONTRACT

    def test_missing_evidence_is_the_sentinel(self):
        assert contract_address_of("large_datum", None) == NO_CONTRACT
        assert contract_address_of("large_datum", {}) == NO_CONTRACT

    def test_missing_key_is_the_sentinel(self):
        # A gated-out or partial finding may omit its own key.
        assert contract_address_of("large_datum", {"large_datum": {}}) == NO_CONTRACT

    def test_non_dict_class_evidence_is_the_sentinel(self):
        assert contract_address_of("large_datum", {"large_datum": "oops"}) == NO_CONTRACT

    def test_non_string_value_is_the_sentinel(self):
        evidence = {"large_datum": {"target_script_address": 12345}}
        assert contract_address_of("large_datum", evidence) == NO_CONTRACT


class TestMapCoversKnownClasses:
    def test_every_mapped_class_is_a_real_attack_class(self):
        valid = {*_CLASS_NAMES, AttackClass.CONTRACT_ANOMALY.value}
        assert set(CONTRACT_EVIDENCE_KEYS) <= valid

    def test_no_identity_classes_are_absent_from_the_map(self):
        for attack_class in _NO_IDENTITY_CLASSES:
            assert attack_class not in CONTRACT_EVIDENCE_KEYS
