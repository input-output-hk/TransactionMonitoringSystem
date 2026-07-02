"""Loader + validator for the notification module configuration.

The config is a single structured document (channels, the band x attack-class
trigger matrix, recipient lists with group aliases, per-rule overrides, and the
periodic-report settings). It is stored in Postgres (one JSONB row) and edited
at runtime through the admin web UI; this module owns loading it into an
in-process cache and validating it.

Secrets never live in the document: SMTP credentials come from the ``SMTP_*``
env vars and the webhook HMAC signing key from ``WEBHOOK_SIGNING_SECRET``. A
validator guard rejects any secret-looking key so the UI can't smuggle one in.

Caching model (IMPORTANT): the accessors below are SYNCHRONOUS and are called
from a ClickHouse executor thread (``on_new_scores`` → ``resolve_dispatch``)
that cannot ``await``. A Postgres read is async. So the only DB access is in
``async refresh_from_db()`` (run at startup + after each admin edit, on the
main loop); the sync accessors read the ``_config`` cache and NEVER do I/O.
This assumes a single uvicorn worker (current deploy) — with multiple workers a
PUT would refresh only one worker's cache; a Postgres LISTEN/NOTIFY-driven
refresh would be needed (out of scope).
"""

import copy
import ipaddress
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.config import settings
from app.models.transaction import AttackClass, RiskBand

logger = logging.getLogger(__name__)

# Bands and attack classes the config may reference (validated against the
# canonical enums so a typo fails at load, not at first alert).
_VALID_BANDS = {b.value for b in RiskBand}
_VALID_CLASSES = {c.value for c in AttackClass}
_VALID_FREQUENCIES = {"daily", "weekly", "monthly"}

# Defaults for the optional periodic_report block.
_REPORT_DEFAULTS = {
    "enabled": False,
    "frequency": "weekly",
    "window_days": 7,
    "channels": ["email"],
    "recipients": [],
    "attack_classes": "all",
    "min_band": "Moderate",
}

_GROUP_PREFIX = "group:"

# Shipped-safe default document, seeded into the DB on first boot: email on,
# webhook off, Critical/High page on all channels, Moderate/Informational
# silent, no periodic report. Operators tune it from here via the admin UI.
_DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "channels": {
        "email": {"enabled": True, "recipients": ["ops@example.com"]},
        "webhook": {"enabled": False, "default_url": ""},
    },
    "groups": {"soc-team": ["ops@example.com"]},
    "triggers": {
        "defaults": {
            "Critical": ["email", "webhook"],
            "High": ["email", "webhook"],
            "Moderate": [],
            "Informational": [],
        },
        "rules": [],
    },
    "periodic_report": dict(_REPORT_DEFAULTS),
}

# Key names that must never appear in the stored document (secrets live in env).
# Matched against a NORMALIZED form (lowercased, separators stripped) so that
# camelCase / snake_case / kebab spellings of the same secret are all caught
# (e.g. ``smtpPassword``, ``webhook_signing_secret``, ``api-key``).
_FORBIDDEN_KEY_NORMS = {
    "password", "passwd", "pass",
    "secret", "secretkey", "signingsecret", "webhooksigningsecret",
    "smtppassword",
    "apikey", "apitoken", "accesstoken", "authtoken", "token",
    "credential", "credentials", "creds",
    "privatekey", "privkey",
}


def _looks_like_secret(key: str) -> bool:
    """True if a config key name looks like it carries a secret value."""
    norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
    # Any SMTP-prefixed key (SMTP is configured entirely via env) or a known
    # secret-bearing name in its normalized form.
    return norm.startswith("smtp") or norm in _FORBIDDEN_KEY_NORMS


# In-process cache of the validated config document. refresh_from_db ALWAYS
# rebinds this to a brand-new object (it never mutates the existing one in
# place), so a sync accessor running on the ClickHouse executor thread observes
# either the whole old document or the whole new one — never a torn read.
# Accessors return references into this dict and MUST treat it as read-only.
_config: Optional[Dict[str, Any]] = None


async def refresh_from_db() -> Dict[str, Any]:
    """Load the stored config from Postgres into the in-process cache.

    Seeds the safe default on first boot. Validates BEFORE caching so a
    malformed stored doc fails startup loudly, not silently at the first alert.
    Runs only on the main event loop (lifespan + the admin PUT handler).
    """
    global _config
    from app.db import postgres  # late import: keep the module import tree flat
    doc = await postgres.get_notification_config()
    if doc is None:
        doc = copy.deepcopy(_DEFAULT_CONFIG)
        await postgres.set_notification_config(doc, "system:seed")
        logger.info("Notification config seeded with safe defaults")
    _validate("notification config", doc)
    _config = doc
    logger.info("Notification config loaded (%d channels)", len(doc.get("channels", {})))
    return _config


def load(force: bool = False) -> Dict[str, Any]:
    """Return the in-memory config cache — NO I/O, safe from any thread.

    The cache is populated by :func:`refresh_from_db` at startup and after each
    admin edit. ``force`` is a no-op kept for signature stability; to reload
    from the DB, ``await refresh_from_db()``. Falls back to the safe default if
    the cache is somehow unpopulated (e.g. an accessor reached before startup).
    """
    return _config if _config is not None else _DEFAULT_CONFIG


def _reject_secret_keys(source: str, node: Any, path: str = "config") -> None:
    """Recursively reject secret-looking keys anywhere in the document."""
    if isinstance(node, dict):
        for k, v in node.items():
            if _looks_like_secret(k):
                raise RuntimeError(
                    f"{source}: key '{path}.{k}' looks like a secret — secrets "
                    "are configured via environment variables, not the config "
                    "document."
                )
            _reject_secret_keys(source, v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _reject_secret_keys(source, item, f"{path}[{i}]")


def _validate(source: str, data: Dict[str, Any]) -> None:
    # ── no smuggled secrets ──
    _reject_secret_keys(source, data)
    # ── version ── (forward-compat guard: only schema v1 is understood)
    version = data.get("version", 1)
    if version != 1:
        raise RuntimeError(
            f"{source}: unsupported config version {version!r} (this build "
            "understands version 1)."
        )
    # ── channels ──
    channels = data.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise RuntimeError(
            f"{source} must contain a non-empty 'channels' mapping."
        )
    for name, spec in channels.items():
        if not isinstance(spec, dict):
            raise RuntimeError(f"{source}: channels.{name} must be a mapping.")
        if not isinstance(spec.get("enabled"), bool):
            raise RuntimeError(f"{source}: channels.{name}.enabled must be a boolean.")

    # ── groups (optional) ──
    groups = data.get("groups") or {}
    if not isinstance(groups, dict):
        raise RuntimeError(f"{source}: 'groups' must be a mapping if present.")
    for gname, members in groups.items():
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise RuntimeError(f"{source}: groups.{gname} must be a list of strings.")

    known_channels = set(channels.keys())

    def _check_channel_list(ref: str, names: Any) -> None:
        if not isinstance(names, list):
            raise RuntimeError(f"{source}: {ref} must be a list of channel names.")
        for n in names:
            if n not in known_channels:
                raise RuntimeError(
                    f"{source}: {ref} references unknown channel '{n}' "
                    f"(known: {sorted(known_channels)})."
                )

    def _check_recipients(ref: str, recips: Any) -> None:
        if not isinstance(recips, list):
            raise RuntimeError(f"{source}: {ref} must be a list.")
        for r in recips:
            if not isinstance(r, str):
                raise RuntimeError(f"{source}: {ref} entries must be strings.")
            if r.startswith(_GROUP_PREFIX) and r[len(_GROUP_PREFIX):] not in groups:
                raise RuntimeError(
                    f"{source}: {ref} references undefined group "
                    f"'{r[len(_GROUP_PREFIX):]}'."
                )

    def _check_webhook_url(ref: str, url: Any) -> None:
        if not isinstance(url, str):
            raise RuntimeError(f"{source}: {ref} must be a string.")
        # Empty means "no URL"; otherwise require an http(s) scheme so a stored
        # doc can't smuggle file://, gopher://, etc. into the egress path.
        if url and not url.startswith(("http://", "https://")):
            raise RuntimeError(
                f"{source}: {ref} must be an http(s) URL (got {url!r})."
            )

    for name, spec in channels.items():
        if "recipients" in spec:
            _check_recipients(f"channels.{name}.recipients", spec["recipients"])
        if "default_url" in spec:
            _check_webhook_url(f"channels.{name}.default_url", spec["default_url"])

    # ── triggers ──
    triggers = data.get("triggers")
    if not isinstance(triggers, dict):
        raise RuntimeError(f"{source} must contain a 'triggers' mapping.")
    defaults = triggers.get("defaults")
    if not isinstance(defaults, dict):
        raise RuntimeError(f"{source}: triggers.defaults must be a mapping.")
    for band, chans in defaults.items():
        if band not in _VALID_BANDS:
            raise RuntimeError(
                f"{source}: triggers.defaults has unknown band '{band}' "
                f"(valid: {sorted(_VALID_BANDS)})."
            )
        _check_channel_list(f"triggers.defaults.{band}", chans)

    rules = triggers.get("rules") or []
    if not isinstance(rules, list):
        raise RuntimeError(f"{source}: triggers.rules must be a list.")
    for i, rule in enumerate(rules):
        ref = f"triggers.rules[{i}]"
        if not isinstance(rule, dict):
            raise RuntimeError(f"{source}: {ref} must be a mapping.")
        if rule.get("band") not in _VALID_BANDS:
            raise RuntimeError(
                f"{source}: {ref}.band is missing or unknown ('{rule.get('band')}')."
            )
        classes = rule.get("attack_classes")
        if not isinstance(classes, list) or not classes:
            raise RuntimeError(f"{source}: {ref}.attack_classes must be a non-empty list.")
        for c in classes:
            if c not in _VALID_CLASSES:
                raise RuntimeError(
                    f"{source}: {ref}.attack_classes has unknown class '{c}' "
                    f"(valid: {sorted(_VALID_CLASSES)})."
                )
        _check_channel_list(f"{ref}.channels", rule.get("channels", []))
        recips_override = rule.get("recipients")
        if recips_override is not None:
            if not isinstance(recips_override, dict):
                raise RuntimeError(
                    f"{source}: {ref}.recipients must be a mapping of channel -> list."
                )
            for cname, rlist in recips_override.items():
                if cname not in known_channels:
                    raise RuntimeError(
                        f"{source}: {ref}.recipients references unknown channel '{cname}'."
                    )
                _check_recipients(f"{ref}.recipients.{cname}", rlist)
        if "webhook_url" in rule:
            _check_webhook_url(f"{ref}.webhook_url", rule["webhook_url"])

    # ── periodic_report (optional) ──
    report = data.get("periodic_report")
    if report is not None:
        if not isinstance(report, dict):
            raise RuntimeError(f"{source}: 'periodic_report' must be a mapping.")
        if not isinstance(report.get("enabled", False), bool):
            raise RuntimeError(f"{source}: periodic_report.enabled must be a boolean.")
        freq = report.get("frequency", "weekly")
        if freq not in _VALID_FREQUENCIES:
            raise RuntimeError(
                f"{source}: periodic_report.frequency must be one of "
                f"{sorted(_VALID_FREQUENCIES)} (got {freq!r})."
            )
        wd = report.get("window_days", 7)
        if not isinstance(wd, int) or isinstance(wd, bool) or wd <= 0:
            raise RuntimeError(
                f"{source}: periodic_report.window_days must be a positive integer."
            )
        _check_channel_list("periodic_report.channels", report.get("channels", []))
        if "recipients" in report:
            _check_recipients("periodic_report.recipients", report["recipients"])
        acl = report.get("attack_classes", "all")
        if acl != "all":
            if not isinstance(acl, list) or not all(isinstance(c, str) for c in acl):
                raise RuntimeError(
                    f"{source}: periodic_report.attack_classes must be 'all' or a "
                    "list of class names."
                )
            for c in acl:
                if c not in _VALID_CLASSES:
                    raise RuntimeError(
                        f"{source}: periodic_report.attack_classes has unknown class "
                        f"'{c}' (valid: {sorted(_VALID_CLASSES)})."
                    )
        mb = report.get("min_band", "Moderate")
        if mb not in _VALID_BANDS:
            raise RuntimeError(
                f"{source}: periodic_report.min_band must be one of "
                f"{sorted(_VALID_BANDS)} (got {mb!r})."
            )


# ── Accessors (sync, cache-only — never do I/O here) ───────────────────

def channels_config() -> Dict[str, Any]:
    return load().get("channels", {})


def triggers_config() -> Dict[str, Any]:
    return load().get("triggers", {})


def channel_enabled(name: str) -> bool:
    spec = channels_config().get(name)
    return bool(spec and spec.get("enabled"))


def channel_recipients(name: str) -> List[str]:
    """Global default recipients for a channel, with group aliases expanded."""
    spec = channels_config().get(name) or {}
    return resolve_recipients(spec.get("recipients", []))


def webhook_default_url() -> str:
    spec = channels_config().get("webhook") or {}
    return spec.get("default_url", "") or ""


def webhook_signing_secret() -> Optional[str]:
    """The HMAC-SHA256 key used to sign webhook request bodies.

    Read from the ``WEBHOOK_SIGNING_SECRET`` setting, which pydantic loads from
    a real env var or the ``.env`` files — the same path as the other secrets
    (e.g. ``SMTP_PASSWORD``)."""
    return settings.WEBHOOK_SIGNING_SECRET or None


def resolve_recipients(recipients: List[str]) -> List[str]:
    """Expand ``group:<alias>`` references and dedupe, preserving order."""
    groups = load().get("groups", {}) or {}
    out: List[str] = []
    seen: set = set()
    for r in recipients or []:
        members = (
            groups.get(r[len(_GROUP_PREFIX):], [])
            if r.startswith(_GROUP_PREFIX)
            else [r]
        )
        for m in members:
            if m not in seen:
                seen.add(m)
                out.append(m)
    return out


def periodic_report_config() -> Dict[str, Any]:
    """The periodic_report block with defaults applied.

    Returns a deep copy so a caller may read/mutate the result freely without
    aliasing the shared (read-only) ``_config`` cache.
    """
    merged = {**_REPORT_DEFAULTS, **(load().get("periodic_report") or {})}
    return copy.deepcopy(merged)


def report_enabled() -> bool:
    return bool(periodic_report_config().get("enabled"))


# Public webhook inspectors — fine for testing, never a destination for real
# alert payloads (full tx data egresses in plaintext to a third party).
_PUBLIC_INSPECTOR_HOSTS = (
    "webhook.site", "requestbin", "pipedream.net", "beeceptor.com", "hookbin.com",
)


def warn_if_webhook_egress_public() -> None:
    """Log a loud warning if the webhook channel is enabled and its default URL
    points somewhere risky — a public inspector (plaintext egress to a third
    party) or an internal/metadata address (the server will call into its own
    network). Called at startup + after each edit. Never blocks: an internal
    webhook is a legitimate destination, the operator just gets told."""
    spec = channels_config().get("webhook") or {}
    if not spec.get("enabled"):
        return
    raw = spec.get("default_url") or ""
    url = raw.lower()
    if any(host in url for host in _PUBLIC_INSPECTOR_HOSTS):
        logger.warning(
            "Webhook channel is ENABLED with default_url pointing at a public "
            "inspector (%s) — full alert payloads (tx hashes, scores) will "
            "egress in plaintext to a third party. Replace it before production.",
            url,
        )
    host = (urlparse(raw).hostname or "").lower()
    if host:
        internal = host == "localhost"
        try:
            ip = ipaddress.ip_address(host)
            internal = internal or ip.is_loopback or ip.is_private or ip.is_link_local
        except ValueError:
            pass  # a hostname, not an IP literal — only the localhost check applies
        if internal:
            logger.warning(
                "Webhook channel is ENABLED with default_url pointing at an "
                "internal/loopback/link-local address (%s) — the server will "
                "issue requests inside its own network. Intended for an internal "
                "receiver? Fine. Otherwise this is a potential SSRF target.",
                host,
            )
