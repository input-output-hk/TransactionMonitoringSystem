"""SMTP delivery for magic-link emails.

Uses :mod:`aiosmtplib` for async send so we don't block the FastAPI
event loop. Three failure-tolerant behaviors built in:

- ``SMTP_ENABLED=False`` or ``SMTP_HOST`` empty → log the link instead
  of sending. Useful during local bootstrap before SMTP is configured.
- Send errors don't propagate to the caller's HTTP response — the
  ``request-link`` endpoint must always return 200 (no user enumeration
  by email-existence side-channel), so an SMTP outage degrades to a
  silent log entry.
- Per-call connection rather than a long-lived pool. Magic-link volume
  is low (a few mails per minute at most), and a fresh connection
  avoids dangling sockets if the SMTP provider drops idle peers.

Templates are plain text. HTML can be layered on later by accepting an
``html`` arg in :func:`send_magic_link` — the wire format already
supports multipart via aiosmtplib.
"""
from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Literal

import aiosmtplib

from app.config import settings

logger = logging.getLogger(__name__)

EmailPurpose = Literal["invite", "login"]


def _build_link(token: str) -> str:
    """Construct the user-facing verification URL.

    Keeps the path consistent with the frontend route registered in
    Phase 4 (``/auth/verify?token=...``). ``APP_BASE_URL`` carries no
    trailing slash by convention.
    """
    base = settings.APP_BASE_URL.rstrip("/")
    return f"{base}/auth/verify?token={token}"


def _render(
    purpose: EmailPurpose, full_name: str, link: str, ttl_minutes: int,
) -> tuple[str, str]:
    """Return ``(subject, body)`` for the given purpose."""
    if purpose == "invite":
        subject = "You're invited to TMS"
        body = (
            f"Hi {full_name},\n\n"
            f"You've been invited to the TMS dashboard.\n"
            f"Click the link below to activate your account and sign in:\n\n"
            f"  {link}\n\n"
            f"This link expires in {ttl_minutes} minutes and can be used only once.\n"
            f"If you weren't expecting this email, you can safely ignore it.\n\n"
            f"— TMS\n"
        )
    else:  # login
        subject = "Your TMS sign-in link"
        body = (
            f"Hi {full_name},\n\n"
            f"Click the link below to sign in to TMS:\n\n"
            f"  {link}\n\n"
            f"This link expires in {ttl_minutes} minutes and can be used only once.\n"
            f"If you didn't request this email, you can safely ignore it.\n\n"
            f"— TMS\n"
        )
    return subject, body


async def send_smtp(msg: EmailMessage) -> bool:
    """Send a prepared message over the configured SMTP transport.

    Never raises — returns True on success, False on any failure (the caller
    decides whether/how to surface it). Shared by magic-link auth and the
    notification email channel (``app.notifications.channels.email``) so the
    TLS/STARTTLS/credential/timeout plumbing lives in exactly one place.
    """
    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER or None,
            password=settings.SMTP_PASSWORD or None,
            use_tls=settings.SMTP_USE_TLS,
            start_tls=settings.SMTP_USE_STARTTLS,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
        )
        return True
    except Exception as e:
        logger.error("SMTP send failed: %s", e)
        return False


# How many local-part characters survive redaction: enough to correlate a
# user's support report against the logs, few enough to not reconstruct
# the address.
_REDACT_KEEP_CHARS = 2


def _redact_email(address: str) -> str:
    """Log-safe form of a recipient address.

    Keeps the first characters of the local part plus the full domain:
    enough to correlate a user's support report against the logs without
    writing the full address (PII, and half of the magic-link credential
    pair) into them.
    """
    local, sep, domain = address.partition("@")
    if not sep:
        return f"{local[:_REDACT_KEEP_CHARS]}***"
    return f"{local[:_REDACT_KEEP_CHARS]}***@{domain}"


async def send_magic_link(
    to_email: str, full_name: str, token: str, purpose: EmailPurpose,
) -> bool:
    """Deliver a magic-link email. Never raises — see module docstring.

    Returns True on successful SMTP delivery, False otherwise (so the
    caller can record a metric without changing user-facing behaviour).
    """
    link = _build_link(token)
    subject, body = _render(
        purpose, full_name, link, settings.MAGIC_LINK_TTL_MINUTES,
    )

    if not settings.SMTP_ENABLED or not settings.SMTP_HOST:
        # The magic link embeds a live login credential, so it must not land
        # in the logs of a real deployment that merely has SMTP unconfigured.
        # Only surface the full link when dev mode is explicitly enabled
        # (TMS_ALLOW_DEV_MODE=1, the same opt-in the open-API fallback uses);
        # otherwise log a redacted notice so the credential never leaks.
        dev_mode = settings.TMS_ALLOW_DEV_MODE.strip() == "1"
        if dev_mode:
            logger.warning(
                "SMTP disabled (dev mode) — magic link for %s (%s): %s",
                to_email, purpose, link,
            )
        else:
            logger.warning(
                "SMTP disabled and not in dev mode — magic link for %s (%s) "
                "was NOT delivered and is redacted from logs; configure SMTP.",
                _redact_email(to_email), purpose,
            )
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    msg["To"] = to_email
    msg.set_content(body)

    # Critical to swallow failures: the public endpoint must not leak whether
    # an email matched a real user via different timings or status codes.
    # send_smtp never raises and returns False on any error.
    # Redacted on success AND failure: a recipient address at INFO level ends
    # up in every log aggregator; the truncated form is enough to correlate.
    if await send_smtp(msg):
        logger.info("Sent %s magic-link email to %s", purpose, _redact_email(to_email))
        return True
    logger.error("SMTP send failed for %s (%s)", _redact_email(to_email), purpose)
    return False
