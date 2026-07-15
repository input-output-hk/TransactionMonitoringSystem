"""Isolated fan-out of a payload to its resolved channels.

Runs on the main event loop (scheduled there by ``on_new_scores``). Three
isolation layers protect the system from a slow or broken channel:
  1. thread decoupling — the caller (the scoring worker thread) schedules this
     coroutine fire-and-forget and never awaits it (see ``on_new_scores``);
  2. ``gather(return_exceptions=True)`` — one channel crashing can't abort the
     others;
  3. per-channel ``asyncio.timeout`` + try/except — a hung channel is bounded.
"""

import asyncio
import logging

from app import audit
from app.config import settings
from app.notifications import registry
from app.notifications.channels.base import Attachment, Dispatch, NotificationResult

logger = logging.getLogger(__name__)


async def dispatch(
    payload,
    dispatches: list[Dispatch],
    attachments: "list[Attachment] | None" = None,
) -> bool:
    """Deliver ``payload`` to every channel in ``dispatches``, fully isolated.

    Returns True iff at least one channel actually delivered (used by the
    immediate-alert path to decide whether to record the dedup claim).
    ``attachments`` (e.g. the periodic report CSV) reach channels that support
    them; others ignore them."""
    if not dispatches:
        return False
    results = await asyncio.gather(
        *(_send_one(d, payload, attachments) for d in dispatches),
        return_exceptions=True,
    )
    sent, failed = [], []
    for r in results:
        if isinstance(r, NotificationResult) and r.ok:
            sent.append(r.channel)
        elif isinstance(r, NotificationResult) and not r.skipped:
            failed.append(r.channel)
    if sent or failed:
        # Best-effort accountability trail (never raises).
        await audit.record(
            event_type="notification",
            action="dispatch",
            entity_type="transaction",
            entity_id=getattr(payload, "tx_hash", ""),
            details={
                "notification_type": getattr(payload, "notification_type", None),
                "risk_band": getattr(payload, "risk_band", None),
                "attack_class": getattr(payload, "attack_class", None),
                "sent": sent,
                "failed": failed,
            },
        )
    return bool(sent)


async def _send_one(d: Dispatch, payload, attachments=None) -> NotificationResult:
    channel = registry.get_channel(d.channel)
    if channel is None or not channel.is_enabled or not channel.handles(payload):
        return NotificationResult(d.channel, ok=False, skipped=True, detail="disabled/unknown")
    try:
        async with asyncio.timeout(settings.NOTIFY_SEND_TIMEOUT_SECONDS):
            result = await channel.send(payload, d.recipients, d.webhook_url, attachments)
    except Exception as e:  # timeout included (TimeoutError subclasses Exception)
        logger.error("notification channel %s errored: %r", d.channel, e)
        return NotificationResult(d.channel, ok=False, detail=repr(e))

    tx = getattr(payload, "tx_hash", "?")
    if result.skipped:
        logger.debug("notification %s skipped for %s: %s", d.channel, tx, result.detail)
    elif result.ok:
        logger.info("notification sent via %s for %s (%s)", d.channel, tx, result.detail)
    else:
        logger.warning("notification via %s failed for %s: %s", d.channel, tx, result.detail)
    return result
