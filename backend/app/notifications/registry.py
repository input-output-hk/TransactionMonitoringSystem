"""Channel registry — the hot-swap point.

``_FACTORY`` maps a channel name to its implementation. To add a channel
(e.g. SMS): implement ``NotificationChannel`` in ``channels/sms.py``, add one
line here, and add an ``sms`` channel block via the admin UI. Nothing else in
the module changes. To remove one: disable it in the config (or its env
switch) — no code change.
"""

from app.notifications.channels.base import NotificationChannel
from app.notifications.channels.email import EmailChannel
from app.notifications.channels.webhook import WebhookChannel

_FACTORY: dict[str, type[NotificationChannel]] = {
    "email": EmailChannel,
    "webhook": WebhookChannel,
    # "sms": SmsChannel,   # <- a future channel slots in here, and only here.
}

_channels: list[NotificationChannel] = []


def build_channels() -> None:
    """Instantiate every known channel once. Called at startup.

    Per-channel ``is_enabled`` is evaluated at dispatch time (not here), so a
    config flag flip takes effect without rebuilding.
    """
    _channels.clear()
    _channels.extend(cls() for cls in _FACTORY.values())


def get_channel(name: str) -> NotificationChannel | None:
    return next((c for c in _channels if c.name == name), None)


def all_channels() -> list[NotificationChannel]:
    return list(_channels)
