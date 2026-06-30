"""Admin notification-config endpoints.

Manages the single notification config document (channels, the band x
attack-class trigger matrix, recipient lists + group aliases, per-rule
overrides, periodic-report settings) at runtime — the DB-backed replacement
for the former ``config/notifications.yaml``.

All routes are gated by :func:`app.auth.deps.require_admin` (a browser session
with ``role='Admin'``; an API key alone is not enough). Secrets (SMTP creds,
the webhook HMAC signing key) live in env and are never read or written here —
only their "configured: yes/no" status is surfaced.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from app import audit
from app.auth.deps import require_admin
from app.config import settings
from app.db import postgres
from app.notifications import config as notif_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications-config"])


class NotificationConfigUpdate(BaseModel):
    """The full config document. Deliberately permissive — the authoritative
    validation is ``notif_config._validate`` (the single source of truth shared
    with the startup/seed path and the executor-thread accessors)."""

    model_config = ConfigDict(extra="allow")

    version: int = 1
    channels: Dict[str, Any] = {}
    groups: Dict[str, Any] = {}
    triggers: Dict[str, Any] = {}
    periodic_report: Optional[Dict[str, Any]] = None


@router.get("/config")
async def get_config(_admin: dict = Depends(require_admin)) -> dict:
    """Return the current config document + read-only secret-status flags."""
    doc = await postgres.get_notification_config()
    if doc is None:
        doc = notif_config.load()  # safe default (also gets seeded at startup)
    return {
        "config": doc,
        "secrets_status": {
            "webhook_signing_secret_configured": bool(settings.WEBHOOK_SIGNING_SECRET),
            "smtp_configured": bool(settings.SMTP_HOST) and settings.SMTP_ENABLED,
        },
    }


@router.put("/config")
async def put_config(
    payload: NotificationConfigUpdate,
    request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    """Validate, persist, and hot-reload the config document.

    The in-process cache is refreshed immediately, so the change takes effect
    on the next alert/report with NO restart (single-worker deploy; see
    ``notifications.config`` for the multi-worker caveat)."""
    doc = {k: v for k, v in payload.model_dump().items() if v is not None}

    # Validate via the single source of truth; 422 with the precise message.
    try:
        notif_config._validate("notification config", doc)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    await postgres.set_notification_config(doc, admin.get("email") or "unknown")
    await notif_config.refresh_from_db()       # rebind the cache to the new doc
    notif_config.warn_if_webhook_egress_public()

    # Accountability: best-effort, matching the admin user-CRUD posture (the
    # route is already gated by require_admin). Never blocks the save.
    await audit.record(
        event_type="config_change",
        action="update",
        entity_type="notification_config",
        entity_id="notifications",
        details={},
        actor=admin.get("email"),
        request=request,
    )
    return {"status": "ok", "config": doc}
