"""Alert grouping: which findings are one situation rather than N alerts.

Per-transaction dedup (``notified_alerts``) answers "have we already told anyone
about THIS transaction". For some attack classes that is the wrong unit. A
contract holding a near-backstop datum re-spends it on every state transition,
and each spend is a new tx_hash, so per-tx dedup never fires and the operator
gets one page per block. Measured on mainnet 2026-07-26: 106 alerts in 67
minutes for a single script, 77% of that window's entire alerting volume.

The unit that matters there is the SCRIPT: an analyst investigating "this
contract is emitting 12.5 KB datums" does not need 106 notifications to start,
and the 106 findings are all in ``tx_class_scores`` either way. So the delivery
path collapses them to one alert per (group, band) per window.

Grouping is opt-in PER CLASS, and deliberately narrow. It is only correct when
repeat findings at the same identity are genuinely the same situation, which is
a claim about the attack class, not a general property. The counter-example that
sets the bound: ``phishing`` findings sharing a sender are NOT one situation,
because each transaction is a different victim, and collapsing them would hide
victims 2..N. Adding a class here therefore needs the same kind of evidence the
``large_datum`` entry has.

What grouping never does is suppress a finding. Every scored transaction is
written and visible in the dashboard regardless; this bounds notification
volume only. The recall guards live in
:func:`app.db.postgres.already_notified_group`: a higher band always breaks
through, and the suppression expires with the window.
"""

from typing import Any

from app.utils.bech32 import payment_credential_or_raw

# attack_class -> the evidence key holding the identity to group on.
#
# large_datum groups on the target script address. A bloat finding is a
# statement about the contract that holds the datum, and the per-script
# baseline cannot discriminate a legitimate fixed-size large-state contract
# (its p50 is clamped by baselines.per_script_p50_cap_spread_fraction, which
# exists so an attacker cannot train a script's median upward to de-sensitise
# the axis), so these findings recur indefinitely and need a volume bound
# rather than a score change.
_GROUP_BY_EVIDENCE_KEY: dict[str, str] = {
    "large_datum": "target_script_address",
}


def group_key(attack_class: str, evidence: Any) -> str | None:
    """The grouping key for a finding, or None when this class is not grouped.

    ``evidence`` is the engine result's evidence mapping, keyed by scorer name
    (``{"large_datum": {...}, "_meta": {...}}``).

    Script addresses are reduced to their payment credential, so the same
    validator reached through different stake credentials groups together: those
    are one contract, and treating them as separate identities would let the
    burst through. Falls back to the raw address when the bech32 decode fails,
    matching how the scorers key per-script aggregation.

    Returns None whenever the identity cannot be established, which routes the
    finding to normal per-transaction delivery. Failing open on grouping is the
    recall-safe direction: the cost is a duplicate alert, not a missed one.
    """
    evidence_key = _GROUP_BY_EVIDENCE_KEY.get(attack_class)
    if evidence_key is None:
        return None
    if not isinstance(evidence, dict):
        return None
    class_evidence = evidence.get(attack_class)
    if not isinstance(class_evidence, dict):
        return None
    identity = class_evidence.get(evidence_key)
    if not isinstance(identity, str) or not identity:
        return None
    # Namespaced by class so two classes grouping on the same address keep
    # independent windows.
    return f"{attack_class}:{payment_credential_or_raw(identity)}"
