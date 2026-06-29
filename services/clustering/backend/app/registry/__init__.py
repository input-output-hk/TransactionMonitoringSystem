"""Offline contract-name lookup against the vendored StricaHQ registry.

``lookup_label`` maps an onboarding target to a human-readable label using only
the vendored snapshot (no network). A policy id is itself the script hash; an
``addr…`` is decoded to its payment credential first. Misses return ``""`` —
the same default the ``contracts.label`` column already carries.

Refresh the snapshot with ``scripts/sync_contract_registry.py``.
"""

from __future__ import annotations

from app.registry.bech32 import payment_credential_hex
from app.registry.loader import label_map

__all__ = ["lookup_label", "script_hash_for"]


def script_hash_for(target: str, target_type: str) -> str | None:
    """Resolve a target to its 56-hex script hash, or ``None`` if not derivable.

    A policy id *is* the script hash; an ``addr…`` is decoded to its payment
    credential. Anything else (unknown type, undecodable address) yields ``None``.
    """
    if target_type == "policy":
        return target.strip().lower()
    if target_type == "address":
        return payment_credential_hex(target)
    return None


def lookup_label(target: str, target_type: str) -> str:
    """Return the registry label for ``target``, or ``""`` if unknown."""
    script_hash = script_hash_for(target, target_type)
    if not script_hash:
        return ""
    return label_map().get(script_hash, "")
