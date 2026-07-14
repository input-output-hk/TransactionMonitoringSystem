"""Channel abstraction + the trigger->dispatch contract.

A channel is hot-swappable: implement :class:`NotificationChannel`, register
it in ``app.notifications.registry``, and add a channel block to the
notification config. The dispatcher, the hook, the trigger engine, and the
payload models never reference a concrete channel — that is the
"unplug Email, drop in SMS" requirement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Union

from app.notifications.payloads import ImmediateAlert, PeriodicReport

# Channels accept ``NotificationPayload`` and may declare via ``handles()``
# which concrete types they deliver. The webhook serialises any of them;
# email renders per ``notification_type``.
NotificationPayload = Union[ImmediateAlert, PeriodicReport]


@dataclass
class Dispatch:
    """One resolved delivery instruction produced by the trigger engine.

    ``recipients`` is the resolved address list for address-based channels
    (email); ``webhook_url`` is the resolved endpoint for URL-based channels.
    Each channel reads whichever it needs and ignores the other.
    """

    channel: str
    recipients: list[str] = field(default_factory=list)
    webhook_url: str | None = None


@dataclass
class Attachment:
    """A file to attach to a delivery (e.g. the periodic report CSV).

    Channels that can't carry attachments (webhook) ignore them.
    """

    filename: str
    content: bytes
    mimetype: str = "text/csv"


@dataclass
class NotificationResult:
    """Outcome of a single channel send. Channels never raise — they return
    this (the dispatcher also traps, as defence in depth)."""

    channel: str
    ok: bool
    detail: str = ""  # "sent" / "http 200" / error string
    skipped: bool = False  # disabled, or no resolved target (not a failure)


class NotificationChannel(ABC):
    """Base class every delivery channel inherits."""

    name: str = ""  # machine name: "email", "webhook", "sms", ...

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether this channel may deliver (env switch AND YAML `enabled`
        AND transport configured). The dispatcher skips disabled channels."""

    def handles(self, payload: NotificationPayload) -> bool:
        """Which payload types this channel delivers. Default: all."""
        return True

    @abstractmethod
    async def send(
        self,
        payload: NotificationPayload,
        recipients: list[str],
        target_url: str | None,
        attachments: list[Attachment] | None = None,
    ) -> NotificationResult:
        """Deliver one payload. MUST NOT raise — return ok=False instead.

        ``attachments`` is honoured by channels that support it (email) and
        ignored by those that don't (webhook)."""
