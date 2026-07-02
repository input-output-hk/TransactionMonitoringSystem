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
from typing import Any, Dict, List, Optional

from app.notifications import config
from app.notifications.channels.base import Dispatch

logger = logging.getLogger(__name__)


def resolve_dispatch(band: str, attack_class: str) -> List[Dispatch]:
    """Return the delivery instructions for an alert of (band, attack_class).

    Any band may page if the config routes it — including Informational, if an
    operator enables it for diagnostics. A band with no configured channels
    resolves to an empty list.
    """
    triggers = config.triggers_config()
    rule = _match_rule(triggers.get("rules") or [], band, attack_class)
    if rule is not None:
        channel_names = rule.get("channels") or []
    else:
        channel_names = (triggers.get("defaults") or {}).get(band) or []

    out: List[Dispatch] = []
    for name in channel_names:
        if not config.channel_enabled(name):
            continue  # disabled or unknown (e.g. a stale name) channel
        recipients = _resolve_recipients(name, rule)
        webhook_url = _resolve_webhook_url(name, rule)
        if not recipients and not webhook_url:
            logger.warning(
                "notification: channel '%s' selected for band=%s class=%s but "
                "has no resolved recipients or URL; skipping (config gap)",
                name, band, attack_class,
            )
            continue
        out.append(Dispatch(channel=name, recipients=recipients, webhook_url=webhook_url))
    return out


def _match_rule(
    rules: List[Dict[str, Any]], band: str, attack_class: str,
) -> Optional[Dict[str, Any]]:
    """The matching rule (band == band AND class listed), or None.

    If several match, the last wins — later rules refine earlier ones (the
    loader warns when authoring overlapping rules)."""
    matched: Optional[Dict[str, Any]] = None
    for rule in rules:
        if rule.get("band") == band and attack_class in (rule.get("attack_classes") or []):
            matched = rule
    return matched


def _resolve_recipients(channel: str, rule: Optional[Dict[str, Any]]) -> List[str]:
    """Per-rule recipient override for this channel, else the channel default."""
    if rule is not None:
        override = (rule.get("recipients") or {}).get(channel)
        if override is not None:
            return config.resolve_recipients(override)
    return config.channel_recipients(channel)


def _resolve_webhook_url(channel: str, rule: Optional[Dict[str, Any]]) -> Optional[str]:
    """Per-rule URL override, else the webhook default. None for non-webhook."""
    if channel != "webhook":
        return None
    if rule is not None and rule.get("webhook_url"):
        return rule["webhook_url"]
    return config.webhook_default_url() or None
