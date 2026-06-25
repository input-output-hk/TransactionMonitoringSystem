"""Loader + validator for the notification module configuration.

Loads ``config/notifications.yaml`` (git-tracked, edited and reviewed like
``config/detection.yaml``). Defines which channels are enabled, the
band x attack-class trigger matrix, recipient lists (with group aliases),
and per-rule overrides.

Secrets never live here: SMTP credentials come from the ``SMTP_*`` env vars,
and the webhook HMAC signing key from ``WEBHOOK_SIGNING_SECRET``. This file
carries structure only.

Loaded lazily and cached on first access; :func:`load` is called explicitly
at startup (``main.lifespan``) so a malformed config fails the boot with a
message naming the file and the offending key — not silently at the first
alert. Mirrors :mod:`app.analysis.scorer_config`.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
import logging
import os

import yaml

from app.config import settings
from app.models.transaction import AttackClass, RiskBand

logger = logging.getLogger(__name__)

_FILENAME = "notifications.yaml"

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

_config: Optional[Dict[str, Any]] = None


def _config_path() -> Path:
    """Locate ``notifications.yaml``, honouring ``TMS_CONFIG_DIR`` (shell wins).

    Mirrors :func:`app.analysis.scorer_config._config_dir`: an explicit
    override wins, otherwise walk upward for the ``config/`` directory.
    """
    override = os.environ.get("TMS_CONFIG_DIR") or settings.TMS_CONFIG_DIR or None
    if override:
        return Path(override) / _FILENAME
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "config" / _FILENAME
        if candidate.exists():
            return candidate
    raise RuntimeError(
        f"Could not locate config/{_FILENAME} relative to {here}. "
        "Set TMS_CONFIG_DIR to override."
    )


def load(force: bool = False) -> Dict[str, Any]:
    """Load + validate the config, caching the result. Raises on any problem."""
    global _config
    if _config is not None and not force:
        return _config
    path = _config_path()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError as e:
        raise RuntimeError(f"Cannot read notification config {path}: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"Notification config {path} must be a YAML mapping.")
    _validate(path, data)
    _config = data
    logger.info("Notification config loaded from %s", path)
    return _config


def _validate(path: Path, data: Dict[str, Any]) -> None:
    # ── version ── (forward-compat guard: only schema v1 is understood)
    version = data.get("version", 1)
    if version != 1:
        raise RuntimeError(
            f"{path}: unsupported config version {version!r} (this build "
            "understands version 1)."
        )
    # ── channels ──
    channels = data.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise RuntimeError(
            f"Notification config {path} must contain a non-empty 'channels' mapping."
        )
    for name, spec in channels.items():
        if not isinstance(spec, dict):
            raise RuntimeError(f"{path}: channels.{name} must be a mapping.")
        if not isinstance(spec.get("enabled"), bool):
            raise RuntimeError(f"{path}: channels.{name}.enabled must be a boolean.")

    # ── groups (optional) ──
    groups = data.get("groups") or {}
    if not isinstance(groups, dict):
        raise RuntimeError(f"{path}: 'groups' must be a mapping if present.")
    for gname, members in groups.items():
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise RuntimeError(f"{path}: groups.{gname} must be a list of strings.")

    known_channels = set(channels.keys())

    def _check_channel_list(ref: str, names: Any) -> None:
        if not isinstance(names, list):
            raise RuntimeError(f"{path}: {ref} must be a list of channel names.")
        for n in names:
            if n not in known_channels:
                raise RuntimeError(
                    f"{path}: {ref} references unknown channel '{n}' "
                    f"(known: {sorted(known_channels)})."
                )

    def _check_recipients(ref: str, recips: Any) -> None:
        if not isinstance(recips, list):
            raise RuntimeError(f"{path}: {ref} must be a list.")
        for r in recips:
            if not isinstance(r, str):
                raise RuntimeError(f"{path}: {ref} entries must be strings.")
            if r.startswith(_GROUP_PREFIX) and r[len(_GROUP_PREFIX):] not in groups:
                raise RuntimeError(
                    f"{path}: {ref} references undefined group "
                    f"'{r[len(_GROUP_PREFIX):]}'."
                )

    for name, spec in channels.items():
        if "recipients" in spec:
            _check_recipients(f"channels.{name}.recipients", spec["recipients"])

    # ── triggers ──
    triggers = data.get("triggers")
    if not isinstance(triggers, dict):
        raise RuntimeError(f"{path} must contain a 'triggers' mapping.")
    defaults = triggers.get("defaults")
    if not isinstance(defaults, dict):
        raise RuntimeError(f"{path}: triggers.defaults must be a mapping.")
    for band, chans in defaults.items():
        if band not in _VALID_BANDS:
            raise RuntimeError(
                f"{path}: triggers.defaults has unknown band '{band}' "
                f"(valid: {sorted(_VALID_BANDS)})."
            )
        _check_channel_list(f"triggers.defaults.{band}", chans)

    rules = triggers.get("rules") or []
    if not isinstance(rules, list):
        raise RuntimeError(f"{path}: triggers.rules must be a list.")
    for i, rule in enumerate(rules):
        ref = f"triggers.rules[{i}]"
        if not isinstance(rule, dict):
            raise RuntimeError(f"{path}: {ref} must be a mapping.")
        if rule.get("band") not in _VALID_BANDS:
            raise RuntimeError(
                f"{path}: {ref}.band is missing or unknown ('{rule.get('band')}')."
            )
        classes = rule.get("attack_classes")
        if not isinstance(classes, list) or not classes:
            raise RuntimeError(f"{path}: {ref}.attack_classes must be a non-empty list.")
        for c in classes:
            if c not in _VALID_CLASSES:
                raise RuntimeError(
                    f"{path}: {ref}.attack_classes has unknown class '{c}' "
                    f"(valid: {sorted(_VALID_CLASSES)})."
                )
        _check_channel_list(f"{ref}.channels", rule.get("channels", []))
        recips_override = rule.get("recipients")
        if recips_override is not None:
            if not isinstance(recips_override, dict):
                raise RuntimeError(
                    f"{path}: {ref}.recipients must be a mapping of channel -> list."
                )
            for cname, rlist in recips_override.items():
                if cname not in known_channels:
                    raise RuntimeError(
                        f"{path}: {ref}.recipients references unknown channel '{cname}'."
                    )
                _check_recipients(f"{ref}.recipients.{cname}", rlist)
        if "webhook_url" in rule and not isinstance(rule["webhook_url"], str):
            raise RuntimeError(f"{path}: {ref}.webhook_url must be a string.")

    # ── periodic_report (optional) ──
    report = data.get("periodic_report")
    if report is not None:
        if not isinstance(report, dict):
            raise RuntimeError(f"{path}: 'periodic_report' must be a mapping.")
        if not isinstance(report.get("enabled", False), bool):
            raise RuntimeError(f"{path}: periodic_report.enabled must be a boolean.")
        freq = report.get("frequency", "weekly")
        if freq not in _VALID_FREQUENCIES:
            raise RuntimeError(
                f"{path}: periodic_report.frequency must be one of "
                f"{sorted(_VALID_FREQUENCIES)} (got {freq!r})."
            )
        wd = report.get("window_days", 7)
        if not isinstance(wd, int) or isinstance(wd, bool) or wd <= 0:
            raise RuntimeError(
                f"{path}: periodic_report.window_days must be a positive integer."
            )
        _check_channel_list("periodic_report.channels", report.get("channels", []))
        if "recipients" in report:
            _check_recipients("periodic_report.recipients", report["recipients"])
        acl = report.get("attack_classes", "all")
        if acl != "all":
            if not isinstance(acl, list) or not all(isinstance(c, str) for c in acl):
                raise RuntimeError(
                    f"{path}: periodic_report.attack_classes must be 'all' or a "
                    "list of class names."
                )
            for c in acl:
                if c not in _VALID_CLASSES:
                    raise RuntimeError(
                        f"{path}: periodic_report.attack_classes has unknown class "
                        f"'{c}' (valid: {sorted(_VALID_CLASSES)})."
                    )
        mb = report.get("min_band", "Moderate")
        if mb not in _VALID_BANDS:
            raise RuntimeError(
                f"{path}: periodic_report.min_band must be one of "
                f"{sorted(_VALID_BANDS)} (got {mb!r})."
            )


# ── Accessors ─────────────────────────────────────────────────────────

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
    """The periodic_report block with defaults applied."""
    return {**_REPORT_DEFAULTS, **(load().get("periodic_report") or {})}


def report_enabled() -> bool:
    return bool(periodic_report_config().get("enabled"))


# Public webhook inspectors — fine for testing, never a destination for real
# alert payloads (full tx data egresses in plaintext to a third party).
_PUBLIC_INSPECTOR_HOSTS = (
    "webhook.site", "requestbin", "pipedream.net", "beeceptor.com", "hookbin.com",
)


def warn_if_webhook_egress_public() -> None:
    """Log a loud warning if the webhook channel is enabled and its default URL
    points at a known public inspector (called once at startup)."""
    spec = channels_config().get("webhook") or {}
    if not spec.get("enabled"):
        return
    url = (spec.get("default_url") or "").lower()
    if any(host in url for host in _PUBLIC_INSPECTOR_HOSTS):
        logger.warning(
            "Webhook channel is ENABLED with default_url pointing at a public "
            "inspector (%s) — full alert payloads (tx hashes, scores) will "
            "egress in plaintext to a third party. Replace it before production.",
            url,
        )
