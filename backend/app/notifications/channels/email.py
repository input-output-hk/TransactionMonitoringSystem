"""Email delivery channel (the primary native channel).

Enabled independently via ``EMAIL_NOTIFY_ENABLED`` + the config `enabled` flag,
so notification email can be unplugged without affecting sign-in emails.
"""

import logging
from email.message import EmailMessage
from typing import List, Optional, Tuple

from app.auth.email import send_smtp
from app.config import settings
from app.notifications import config
from app.notifications.channels.base import (
    Attachment,
    NotificationChannel,
    NotificationPayload,
    NotificationResult,
)

logger = logging.getLogger(__name__)

# Canonical band -> human-facing label. The domain enum, the stored data, and
# the webhook JSON payload all keep the canonical "Moderate"; only the text a
# human reads in an email says "Medium", matching the dashboard (which maps it
# the same way). Display-only — never use for the wire payload or config keys.
_BAND_DISPLAY = {"Moderate": "Medium"}


def _band_label(band: str) -> str:
    return _BAND_DISPLAY.get(band, band)


def _render_immediate(payload) -> Tuple[str, str]:
    """(subject, plain-text body) for an immediate alert."""
    band = _band_label(payload.risk_band)
    subject = (
        f"[TMS {band}] {payload.attack_class}: {payload.risk_score:.0f}/100"
    )
    if payload.contributing_features:
        feats = "\n".join(
            f"    - {k}: {v:.2f}" for k, v in payload.contributing_features.items()
        )
    else:
        feats = "    (none)"
    body = (
        f"A {band}-band {payload.attack_class} alert was detected on "
        f"{payload.network}.\n\n"
        f"  Risk score  : {payload.risk_score:.2f} / 100  ({band})\n"
        f"  Attack class: {payload.attack_class}\n"
        f"  Transaction : {payload.tx_hash}\n"
        f"  Network     : {payload.network}\n"
        f"  Baseline    : {payload.baseline_source}\n"
        f"  Detected at : {payload.timestamp}\n\n"
        f"  Top contributing features:\n{feats}\n\n"
        f"  View in dashboard: {payload.dashboard_url}\n\n"
        f"-- TMS Alerting\n"
    )
    return subject, body


def _render_report(payload) -> Tuple[str, str]:
    """(subject, plain-text body) for a periodic report."""
    s = payload.summary
    win = payload.report_window
    subject = (
        f"[TMS] Periodic report: {payload.network} "
        f"({win.get('from', '')[:10]} → {win.get('to', '')[:10]})"
    )
    by_band = "  ".join(
        f"{_band_label(b)}={s.alerts_by_band.get(b, 0)}"
        for b in ("Critical", "High", "Moderate", "Informational")
    )
    classes = "\n".join(
        f"    - {cls}: {n}" for cls, n in s.alerts_by_class.items() if n
    ) or "    (none in window)"
    if payload.top_alerts:
        tops = "\n".join(
            f"    {i + 1:>2}. {_band_label(a.risk_band):<13} {a.risk_score:6.2f}  "
            f"{a.attack_class:<14} {a.tx_hash}"
            for i, a in enumerate(payload.top_alerts)
        )
    else:
        tops = "    (none in window)"
    body = (
        f"TMS periodic report for {payload.network}.\n"
        f"Window: {win.get('from', '')} → {win.get('to', '')}\n\n"
        f"  Transactions scored      : {s.total_transactions_scored}\n"
        f"  Alerts by band           : {by_band}\n"
        f"  False positives archived : {s.false_positives_archived}\n\n"
        f"  Alerts by attack class:\n{classes}\n\n"
        f"  Top alerts:\n{tops}\n\n"
        f"  Open dashboard: {payload.dashboard_url}\n\n"
        f"-- TMS Alerting\n"
    )
    return subject, body


def _render(payload) -> Tuple[str, str]:
    """Dispatch to the renderer for this payload's notification_type."""
    if getattr(payload, "notification_type", None) == "periodic_report":
        return _render_report(payload)
    return _render_immediate(payload)


class EmailChannel(NotificationChannel):
    name = "email"

    @property
    def is_enabled(self) -> bool:
        # Both layers must agree, and the SMTP transport must be configured.
        return (
            settings.EMAIL_NOTIFY_ENABLED
            and config.channel_enabled("email")
            and settings.SMTP_ENABLED
            and bool(settings.SMTP_HOST)
        )

    async def send(
        self,
        payload: NotificationPayload,
        recipients: List[str],
        target_url: Optional[str] = None,
        attachments: Optional[List[Attachment]] = None,
    ) -> NotificationResult:
        if not recipients:
            return NotificationResult(
                self.name, ok=False, skipped=True, detail="no recipients configured"
            )
        subject, body = _render(payload)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)
        for att in attachments or []:
            maintype, _, subtype = att.mimetype.partition("/")
            msg.add_attachment(
                att.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=att.filename,
            )
        ok = await send_smtp(msg)
        return NotificationResult(
            self.name, ok=ok, detail="sent" if ok else "smtp send failed"
        )
