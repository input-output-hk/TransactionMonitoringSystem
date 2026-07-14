"""Audit-trail writes for state-mutating API actions.

The audit_logs table existed since the first schema but nothing ever wrote
to it, so alert suppression (archiving) was unaccountable: a key holder
could hide a real detection with no trace (audit finding). Every mutating
endpoint now records who-did-what-from-where here.

Two write modes:

- ``record`` is best-effort: an audit insert failure is logged loudly but
  never fails the user's request. Right for low-impact events (entity
  state) where losing one row is acceptable.
- ``record_fail_closed`` is for alert suppression, the highest-impact
  mutation in a monitoring system: it records the INTENT before the
  mutation runs and raises ``AuditUnavailableError`` if the row cannot be
  written, so a suppression can never happen unaudited (an attacker who
  could force the audit write to fail would otherwise hide a real
  detection silently). The endpoint then patches the outcome in
  best-effort via ``append_outcome``.

The actor string is client-supplied today (the UI is a demo without
server-side accounts); the server-derived IP (validated in app.net, so a
forged header cannot poison it or crash the ::inet cast) and timestamp
make the trail tamper-evident enough for triage until real authentication
lands.
"""

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from fastapi import Request

from app.db import postgres
from app.net import client_ip

logger = logging.getLogger(__name__)

# A raw API key is a secret and must never be written to the audit trail, so a
# key principal is reduced to a short SHA-256 prefix. 12 hex chars (48 bits) is
# enough to tell a handful of configured keys apart in the log while staying a
# one-way fingerprint, never the key itself.
_ACTOR_FINGERPRINT_LEN = 12


def actor_from_principal(principal: Optional[str]) -> str:
    """Map the credential string returned by ``verify_api_key`` to a
    non-sensitive, server-authoritative actor label for the audit trail.

    ``verify_api_key`` returns one of: ``"dev-mode"``, ``"session:<user_id>"``,
    or the raw API key. The first two are already safe identifiers; the raw key
    is a secret, so it is fingerprinted rather than stored. This label is the
    authenticated principal, not the client-supplied ``archived_by`` field, so
    it cannot be spoofed by the request body.
    """
    if not principal:
        return "unknown"
    if principal == "dev-mode" or principal.startswith("session:"):
        return principal
    digest = hashlib.sha256(principal.encode()).hexdigest()[:_ACTOR_FINGERPRINT_LEN]
    return f"api-key:{digest}"


class AuditUnavailableError(Exception):
    """The audit trail cannot be written; fail-closed actions must abort."""


async def record(
    event_type: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: Dict[str, Any],
    request: Optional[Request] = None,
    actor: Optional[str] = None,
) -> None:
    """Write one audit row best-effort; never raises.

    ``actor`` is the server-derived authenticated principal (see
    ``actor_from_principal``); it is written authoritatively into the details
    and overrides any client-supplied ``actor`` key.
    """
    try:
        await postgres.insert_audit_log(
            event_type=event_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps({**details, "actor": actor}),
            ip_address=client_ip(request),
        )
    except Exception:
        logger.exception(
            "AUDIT WRITE FAILED for %s/%s on %s:%s (action still applied)",
            event_type,
            action,
            entity_type,
            entity_id,
        )


async def record_fail_closed(
    event_type: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: Dict[str, Any],
    request: Optional[Request] = None,
    actor: Optional[str] = None,
) -> int:
    """Write one audit row or raise AuditUnavailableError.

    Returns the audit row id so the caller can patch the outcome in with
    ``append_outcome`` after the audited mutation completes. ``actor`` is the
    server-derived authenticated principal, written authoritatively into the
    details (it overrides any client-supplied ``actor`` key).
    """
    try:
        return await postgres.insert_audit_log(
            event_type=event_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps({**details, "actor": actor}),
            ip_address=client_ip(request),
        )
    except Exception as e:
        logger.exception(
            "AUDIT WRITE FAILED for %s/%s on %s:%s (action REFUSED)",
            event_type,
            action,
            entity_type,
            entity_id,
        )
        raise AuditUnavailableError(str(e)) from e


async def append_outcome(audit_id: int, outcome: Dict[str, Any]) -> None:
    """Best-effort merge of the mutation outcome into an intent audit row."""
    try:
        await postgres.update_audit_log_details(audit_id, json.dumps(outcome))
    except Exception:
        logger.exception("Failed to append outcome to audit row %d", audit_id)
