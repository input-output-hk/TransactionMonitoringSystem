"""Audit-trail writes for state-mutating API actions.

The audit_logs table existed since the first schema but nothing ever wrote
to it, so alert suppression (archiving) was unaccountable: a key holder
could hide a real detection with no trace (audit finding). Every mutating
endpoint now records who-did-what-from-where here.

Writes are best-effort by design: an audit insert failure is logged loudly
but never fails the user's request (the action itself already succeeded or
failed on its own merits). The actor string is client-supplied today (the
UI is a demo without server-side accounts); the server-derived IP and
timestamp make the trail tamper-evident enough for triage until real
authentication lands.
"""

import json
import logging
from typing import Any, Dict, Optional

from fastapi import Request

from app.config import settings
from app.db import postgres

logger = logging.getLogger(__name__)


def client_ip(request: Optional[Request]) -> Optional[str]:
    """Best-effort client IP for the audit row.

    X-Forwarded-For is attacker-controlled unless a trusted reverse proxy
    sets it, so it is honoured only when TRUSTED_PROXY_ENABLED is on; the
    first (client-most) entry is used per convention.
    """
    if request is None or request.client is None:
        return None
    if settings.TRUSTED_PROXY_ENABLED:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host


async def record(
    event_type: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: Dict[str, Any],
    request: Optional[Request] = None,
) -> None:
    """Write one audit row; never raises (the audited action stands alone)."""
    try:
        await postgres.insert_audit_log(
            event_type=event_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps(details),
            ip_address=client_ip(request),
        )
    except Exception:
        logger.exception(
            "AUDIT WRITE FAILED for %s/%s on %s:%s (action still applied)",
            event_type, action, entity_type, entity_id,
        )
