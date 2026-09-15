"""Trigger-rule evaluation.

:func:`resolve_dispatch` answers, for one alert, *which channels fire and to
which recipients / URL*, honouring band defaults, per-class scoping, and
per-rule overrides. It is a pure function of the loaded config, so it is
unit-testable against the band x attack-class matrix.

Precedence:
  - the last matching rule in file order (same band AND the alert's
    attack_class listed) overrides the band default;
  - within a channel, a per-rule recipient/URL override beats the channel's
    global default;
  - a band/class with no configured channels resolves to no delivery.

Only outbound channels exist here. Whether a band is visible in the dashboard
is a UI concern (its severity filter), independent of this module.
"""

import logging
from typing import Any

from app.notifications import config
from app.notifications.channels.base import Dispatch

logger = logging.getLogger(__name__)


def _selection(band: str, attack_class: str) -> tuple[dict[str, Any] | None, list[str]]:
    """The matching rule and the channel names it selects, before any channel
    is checked for enablement or resolved to a destination."""
    triggers = config.triggers_config()
    rule = _match_rule(triggers.get("rules") or [], band, attack_class)
    if rule is not None:
        channel_names = rule.get("channels") or []
    else:
        channel_names = (triggers.get("defaults") or {}).get(band) or []
    return rule, list(channel_names)


def selected_channels(band: str, attack_class: str) -> list[str]:
    """The channels the config SELECTS for (band, attack_class), unresolved.

    :func:`resolve_dispatch` returns nothing in two situations that look alike
    and mean opposite things: the config selects no channel at all (the
    operator silenced this band or class), or it selects channels that cannot
    deliver right now (disabled, or with no resolved recipients or URL, the
    "config gap" logged below). A caller holding an undelivered alert has to
    tell those apart before deciding whether to drop it, so this reports the
    selection without resolving it.
    """
    return _selection(band, attack_class)[1]


def resolve_dispatch(band: str, attack_class: str) -> list[Dispatch]:
    """Return the delivery instructions for an alert of (band, attack_class).

    Any band may page if the config routes it — including Informational, if an
    operator enables it for diagnostics. A band with no configured channels
    resolves to an empty list.
    """
    rule, channel_names = _selection(band, attack_class)

    out: list[Dispatch] = []
    for name in channel_names:
        if not config.channel_enabled(name):
            continue  # disabled or unknown (e.g. a stale name) channel
        recipients = _resolve_recipients(name, rule)
        webhook_url = _resolve_webhook_url(name, rule)
        if not recipients and not webhook_url:
            logger.warning(
                "notification: channel '%s' selected for band=%s class=%s but "
                "has no resolved recipients or URL; skipping (config gap)",
                name,
                band,
                attack_class,
            )
            continue
        out.append(Dispatch(channel=name, recipients=recipients, webhook_url=webhook_url))
    return out


def _match_rule(
    rules: list[dict[str, Any]],
    band: str,
    attack_class: str,
) -> dict[str, Any] | None:
    """The matching rule (band == band AND class listed), or None.

    If several match, the last wins — later rules refine earlier ones (the
    loader warns when authoring overlapping rules)."""
    matched: dict[str, Any] | None = None
    for rule in rules:
        if rule.get("band") == band and attack_class in (rule.get("attack_classes") or []):
            matched = rule
    return matched


def _resolve_recipients(channel: str, rule: dict[str, Any] | None) -> list[str]:
    """Per-rule recipient override for this channel, else the channel default."""
    if rule is not None:
        override = (rule.get("recipients") or {}).get(channel)
        if override is not None:
            return config.resolve_recipients(override)
    return config.channel_recipients(channel)


def _resolve_webhook_url(channel: str, rule: dict[str, Any] | None) -> str | None:
    """Per-rule URL override, else the webhook default. None for non-webhook."""
    if channel != "webhook":
        return None
    if rule is not None and rule.get("webhook_url"):
        return rule["webhook_url"]
    return config.webhook_default_url() or None
