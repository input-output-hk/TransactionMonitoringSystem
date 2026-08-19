"""Normalized contract identity for a scored transaction.

Every scorer that can name the on-chain contract it implicates does so under
its own evidence key: there is no shared field, because each scorer reaches the
address by a different route (the script whose UTxOs were spent, the script
holding the bloated datum, the sidecar's watched target). This module collapses
those per-class keys onto one value, so a caller can group alerts by contract
without knowing which scorer won.

Only keys carrying a *bech32 address* are mapped. Two other identifier-shaped
evidence keys are deliberately excluded:

- ``sandwich.pool_id`` is a DEX pool identifier, not an address.
- ``fake_token.fake_policy_id`` is a minting policy id, not an address.

Folding either into this column would put two identifier namespaces in one
field, so a consumer could not tell whether to resolve a value as an address
(and an address-explorer link built from a policy id would simply be wrong).
Alerts from those classes therefore report "no contract identity", which is
accurate rather than convenient.
"""

from typing import Any, Final

from app.models.transaction import AttackClass

# The evidence key naming the implicated contract, per attack class. A class
# absent from this map carries no contract identity at all: phishing, circular
# and front_running describe a relationship between addresses rather than an
# interaction with one contract, so there is nothing to group them by.
CONTRACT_EVIDENCE_KEYS: Final[dict[str, str]] = {
    AttackClass.TOKEN_DUST.value: "target_script_address",
    AttackClass.LARGE_VALUE.value: "target_script_address",
    AttackClass.LARGE_DATUM.value: "target_script_address",
    AttackClass.MULTIPLE_SAT.value: "target_script_address",
    # The sidecar's watched target. Not necessarily a script address: the
    # clustering registry also watches plain payment addresses (its
    # contracts.target_type column records which), and both are legitimate
    # things to group an operator's alerts by.
    AttackClass.CONTRACT_ANOMALY.value: "target",
}

# Sentinel for "this alert names no contract". Chosen over NULL because the
# ClickHouse column is a plain String with DEFAULT '' (see clickhouse_schema),
# so historical rows predating the column already read as this value and no
# reader needs to special-case a nullable.
NO_CONTRACT: Final[str] = ""


def contract_address_of(max_class: str, evidence: dict[str, Any] | None) -> str:
    """The contract address implicated by an alert's winning class.

    Returns :data:`NO_CONTRACT` when the class carries no contract identity,
    when the scorer did not emit its key (a gated-out or partial finding), or
    when the value is not a usable string.

    ``evidence`` is the full per-class evidence mapping as persisted, i.e.
    ``{class_name: {key: value}}``, not a single class's sub-dict.
    """
    key = CONTRACT_EVIDENCE_KEYS.get(max_class)
    if key is None or not evidence:
        return NO_CONTRACT
    class_evidence = evidence.get(max_class)
    if not isinstance(class_evidence, dict):
        return NO_CONTRACT
    value = class_evidence.get(key)
    if not isinstance(value, str):
        return NO_CONTRACT
    return value.strip()
