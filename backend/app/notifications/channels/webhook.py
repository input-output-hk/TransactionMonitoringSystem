"""Webhook delivery channel.

HTTP POST of the payload as JSON — the body is exactly the payload record
(``payload.model_dump(mode="json")``), the recommended integration path for
mobile push / Telegram / Slack / SIEM. Bounded retry on transient (5xx /
network) errors; a 4xx is treated as a permanent client error and not
retried. The dispatcher's per-channel timeout is the hard ceiling.

When a signing secret is configured, the request body is signed with
HMAC-SHA256 and the signature sent in the ``X-TMS-Signature`` header as
``sha256=<hexdigest>``. We POST the exact bytes we signed, so the receiver can
verify with ``hmac_sha256(secret, request_body)`` — the secret never travels.
Receivers MUST compare with a constant-time function (e.g.
``hmac.compare_digest``) to avoid a timing oracle.
"""

import asyncio
import hashlib
import hmac
import json
import logging
from typing import List, Optional

import httpx

from app.config import settings
from app.notifications import config
from app.notifications.channels.base import (
    NotificationChannel,
    NotificationPayload,
    NotificationResult,
)

logger = logging.getLogger(__name__)

# Header carrying the HMAC-SHA256 signature of the raw request body.
_SIGNATURE_HEADER = "X-TMS-Signature"


class WebhookChannel(NotificationChannel):
    name = "webhook"

    @property
    def is_enabled(self) -> bool:
        return settings.WEBHOOK_NOTIFY_ENABLED and config.channel_enabled("webhook")

    async def send(
        self,
        payload: NotificationPayload,
        recipients: Optional[List[str]] = None,
        target_url: Optional[str] = None,
        attachments=None,  # webhook delivers the JSON payload; attachments N/A
    ) -> NotificationResult:
        url = target_url or config.webhook_default_url()
        if not url:
            return NotificationResult(
                self.name, ok=False, skipped=True, detail="no endpoint URL configured"
            )

        # Serialize ourselves and POST the exact bytes we sign (not httpx's
        # json=, whose separators we don't control) so the receiver can verify
        # the HMAC over the body it actually received.
        raw = json.dumps(
            payload.model_dump(mode="json"), separators=(",", ":")
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = config.webhook_signing_secret()
        if secret:
            signature = hmac.new(
                secret.encode("utf-8"), raw, hashlib.sha256
            ).hexdigest()
            headers[_SIGNATURE_HEADER] = f"sha256={signature}"

        attempts = max(1, settings.WEBHOOK_MAX_RETRIES + 1)
        last = "no attempt made"
        # One client for all attempts: connection-pool and TLS reuse across
        # retries. Each attempt is still bounded by the per-request timeout.
        async with httpx.AsyncClient(
            timeout=settings.WEBHOOK_TIMEOUT_SECONDS
        ) as client:
            for i in range(attempts):
                try:
                    resp = await client.post(url, content=raw, headers=headers)
                    if resp.status_code < 400:
                        return NotificationResult(
                            self.name, ok=True, detail=f"http {resp.status_code}"
                        )
                    last = f"http {resp.status_code}"
                    # 4xx is a permanent client error: don't waste retries on it.
                    if resp.status_code < 500:
                        return NotificationResult(self.name, ok=False, detail=last)
                except Exception as e:  # network / DNS / TLS / timeout
                    last = repr(e)
                if i < attempts - 1:
                    await asyncio.sleep(
                        settings.WEBHOOK_RETRY_BACKOFF_SECONDS * (i + 1)
                    )
        return NotificationResult(self.name, ok=False, detail=last)
