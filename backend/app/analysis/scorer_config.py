"""Loader for client-tunable detection scorer configuration.

Loads ``config/detection.yaml`` at import time. The file is tracked in git:
edits to detection thresholds, weights, and allowlists are versioned and
reviewable. Scorers read their section via :func:`get`.

The config is intentionally a plain nested-dict structure so clients can edit
YAML without running a validation toolchain. A shallow structural check runs
at load time so a missing or misnamed key fails with an error that names the
file and the key path, not a deep ``KeyError`` from inside a scorer module.
"""

from pathlib import Path
from typing import Any, Dict, Iterable, Tuple
import logging
import os

import yaml

logger = logging.getLogger(__name__)

# Required top-level keys for each scorer section. Extend the set when a
# scorer starts reading a new block. Nested key validation is left to the
# scorer itself (KeyError there still beats a silent wrong value).
_REQUIRED_KEYS: Dict[str, Tuple[str, ...]] = {
    "multiple_sat":  ("weights", "bootstrap_anchors", "allowlist_prefixes", "reason_threshold"),
    "large_datum":   ("gate", "weights", "fixed_anchors", "bootstrap_anchors", "reason_threshold"),
    "token_dust":    ("weights", "bootstrap_anchors", "reason_threshold"),
    "large_value":   ("weights", "bootstrap_anchors", "reason_threshold"),
    "front_running": ("weights", "fixed_anchors", "bootstrap_anchors", "outcome_scores",
                      "reason_thresholds", "min_recurrence_wins", "high_band_cap",
                      "delta_ms_default"),
    "sandwich":      ("weights", "fixed_anchors", "bootstrap_anchors", "link_scores",
                      "window_slots", "min_profit_lovelace", "high_band_cap",
                      "reason_thresholds"),
    "circular":      ("weights", "fixed_anchors", "bootstrap_anchors", "cycle",
                      "reason_threshold", "moderate_cap"),
    "fake_token":    ("weights", "fixed_anchors", "bootstrap_anchors",
                      "similarity_threshold", "unicode_scores", "reason_thresholds"),
    "phishing":      ("weights", "fixed_anchors", "bootstrap_anchors",
                      "similarity_suspicious_range", "social_engineering",
                      "reason_thresholds", "critical_threshold", "metadata_labels"),
}


def _config_dir() -> Path:
    """Resolve the config directory, honouring TMS_CONFIG_DIR if set."""
    override = os.environ.get("TMS_CONFIG_DIR")
    if override:
        return Path(override)
    # Walk upward until we find a directory containing `config/detection.yaml`.
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "config" / "detection.yaml"
        if candidate.exists():
            return candidate.parent
    raise RuntimeError(
        "Could not locate config/detection.yaml relative to "
        f"{here}. Set TMS_CONFIG_DIR to override."
    )


def _validate(path: Path, data: Dict[str, Any]) -> None:
    if "scorers" not in data or not isinstance(data["scorers"], dict):
        raise RuntimeError(
            f"Detection config {path} must contain a top-level 'scorers' mapping."
        )
    scorers = data["scorers"]
    missing: Iterable[str] = []
    for name, keys in _REQUIRED_KEYS.items():
        section = scorers.get(name)
        if section is None:
            missing = list(missing) + [f"scorers.{name}"]
            continue
        if not isinstance(section, dict):
            raise RuntimeError(
                f"Detection config {path}: scorers.{name} must be a mapping."
            )
        for key in keys:
            if key not in section:
                missing = list(missing) + [f"scorers.{name}.{key}"]
    if missing:
        joined = ", ".join(sorted(missing))
        raise RuntimeError(
            f"Detection config {path} missing required keys: {joined}"
        )


def _load() -> Dict[str, Any]:
    path = _config_dir() / "detection.yaml"
    if not path.exists():
        raise RuntimeError(f"Detection config not found at {path}.")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _validate(path, data)
    logger.info(f"Detection config loaded from {path.name}")
    return data


_CFG: Dict[str, Any] = _load()


def get(section: str) -> Dict[str, Any]:
    """Return the config section for a given scorer (e.g. ``'multiple_sat'``)."""
    cfg = _CFG["scorers"].get(section)
    if cfg is None:
        raise KeyError(
            f"No config section for scorer '{section}'. "
            f"Add a 'scorers.{section}' block to detection.yaml."
        )
    return cfg


def anchor(container: Dict[str, Any], key: str) -> Tuple[float, float]:
    """Extract ``(p50, p99)`` from a ``{key: {p50: ..., p99: ...}}`` mapping."""
    a = container[key]
    return float(a["p50"]), float(a["p99"])
