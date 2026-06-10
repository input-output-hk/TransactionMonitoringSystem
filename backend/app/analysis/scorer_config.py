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
from typing import Any, Dict, Iterable, List, Optional, Tuple
import logging
import os

import yaml

from app.analysis.normalise import resolve_baseline
from app.config import settings

logger = logging.getLogger(__name__)

# Required top-level keys for each scorer section. Extend the set when a
# scorer starts reading a new block. Nested key validation is left to the
# scorer itself (KeyError there still beats a silent wrong value).
_REQUIRED_KEYS: Dict[str, Tuple[str, ...]] = {
    "multiple_sat":  ("weights", "bootstrap_anchors", "allowlist_prefixes", "reason_threshold",
                      "lazy_validator_threshold", "lazy_validator_floor",
                      "lazy_validator_extraction_min",
                      "per_script_extraction_headroom",
                      "uniform_sweep_guard.enabled",
                      "uniform_sweep_guard.require_uniform_redeemer",
                      "uniform_sweep_guard.require_no_script_return",
                      "uniform_sweep_guard.min_inputs",
                      "suppression_escape.enabled",
                      "suppression_escape.extraction_floor_min"),
    "large_datum":   ("gate", "gate.flag_datum_hash_only",
                      "weights", "fixed_anchors", "bootstrap_anchors",
                      "aggregate_engagement_min", "reason_threshold"),
    "token_dust":    ("gate.min_token_count", "weights", "bootstrap_anchors",
                      "allowlist_prefixes", "allowlist_policies",
                      "dos_asset_min", "reason_threshold"),
    "large_value":   ("weights", "bootstrap_anchors", "reason_threshold",
                      "min_digits_subscore"),
    "front_running": ("weights", "fixed_anchors", "bootstrap_anchors", "outcome_scores",
                      "reason_thresholds", "min_recurrence_wins", "high_band_cap",
                      "delta_ms_default"),
    "sandwich":      ("weights", "fixed_anchors", "bootstrap_anchors", "link_scores",
                      "window_slots", "neighbor_limit", "min_profit_lovelace",
                      "reason_thresholds"),
    "circular":      ("weights", "fixed_anchors", "bootstrap_anchors", "cycle",
                      "reason_threshold", "moderate_cap"),
    "fake_token":    ("weights", "fixed_anchors", "bootstrap_anchors",
                      "similarity_threshold", "unicode_scores", "reason_thresholds",
                      "critical_assets.multiplier", "critical_assets.names",
                      "ascii_homoglyphs_enabled"),
    "phishing":      ("weights", "fixed_anchors", "bootstrap_anchors",
                      "similarity_suspicious_range", "social_engineering",
                      "reason_thresholds", "critical_threshold", "metadata_labels",
                      "asset_name_carrier", "min_decoded_string_len"),
}


def _config_dir() -> Path:
    """Resolve the config directory, honouring TMS_CONFIG_DIR if set.

    TMS_CONFIG_DIR can come from .env (via pydantic settings) or the shell
    environment; shell wins when both are present.
    """
    override = (
        os.environ.get("TMS_CONFIG_DIR")
        or settings.TMS_CONFIG_DIR
        or None
    )
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


# Required top-level protocol parameters. These encode the Cardano ledger
# resource limits that several scorers' thresholds are derived from; a missing
# block must fail loudly at import rather than surfacing as a KeyError deep
# inside a scorer.
_REQUIRED_PROTOCOL_LIMITS: Tuple[str, ...] = (
    "max_value_size_bytes",
    "max_tx_size_bytes",
)

# Required keys for the top-level composite_corroboration block (cross-class
# agreement signal; see detection.yaml). Top-level, not a scorer section.
_REQUIRED_COMPOSITE_CORROBORATION: Tuple[str, ...] = (
    "corroboration_threshold",
)

_REQUIRED_BASELINES: Tuple[str, ...] = (
    "min_spread_ratio",
)


def _validate(path: Path, data: Dict[str, Any]) -> None:
    if "scorers" not in data or not isinstance(data["scorers"], dict):
        raise RuntimeError(
            f"Detection config {path} must contain a top-level 'scorers' mapping."
        )
    limits = data.get("protocol_limits")
    if not isinstance(limits, dict):
        raise RuntimeError(
            f"Detection config {path} must contain a top-level 'protocol_limits' mapping."
        )
    missing_limits = [k for k in _REQUIRED_PROTOCOL_LIMITS if k not in limits]
    if missing_limits:
        raise RuntimeError(
            f"Detection config {path} missing protocol_limits keys: "
            f"{', '.join(missing_limits)}"
        )
    corroboration = data.get("composite_corroboration")
    if not isinstance(corroboration, dict):
        raise RuntimeError(
            f"Detection config {path} must contain a top-level "
            f"'composite_corroboration' mapping."
        )
    missing_corr = [
        k for k in _REQUIRED_COMPOSITE_CORROBORATION if k not in corroboration
    ]
    if missing_corr:
        raise RuntimeError(
            f"Detection config {path} missing composite_corroboration keys: "
            f"{', '.join(missing_corr)}"
        )
    baselines = data.get("baselines")
    if not isinstance(baselines, dict):
        raise RuntimeError(
            f"Detection config {path} must contain a top-level 'baselines' mapping."
        )
    missing_bl = [k for k in _REQUIRED_BASELINES if k not in baselines]
    if missing_bl:
        raise RuntimeError(
            f"Detection config {path} missing baselines keys: "
            f"{', '.join(missing_bl)}"
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
            # Dotted keys ("a.b.c") walk into nested dicts so callers can
            # require leaf tunables, not just top-level blocks. Reports the
            # full path in the error so YAML edits surface the precise
            # missing field rather than a downstream KeyError at import.
            cur: Any = section
            parts = key.split(".")
            for part in parts:
                if not isinstance(cur, dict) or part not in cur:
                    missing = list(missing) + [f"scorers.{name}.{key}"]
                    break
                cur = cur[part]
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


def baselines_config() -> Dict[str, Any]:
    """Return the top-level baselines block (resolution tunables shared by
    every percentile-baselined scorer). Presence and required keys are
    enforced at load time by :func:`_validate`.
    """
    return _CFG["baselines"]


def composite_corroboration_config() -> Dict[str, Any]:
    """Return the top-level composite_corroboration block.

    Cross-class agreement signal (not a scorer section). Presence and required
    keys are enforced at load time by :func:`_validate`.
    """
    return _CFG["composite_corroboration"]


def protocol_limit(name: str) -> int:
    """Return a Cardano ledger protocol limit (e.g. ``'max_tx_size_bytes'``).

    These are top-level (not per-scorer) so multiple scorers derive byte
    thresholds from the same named value instead of repeating raw constants.
    Presence is enforced at load time by :func:`_validate`.
    """
    limits = _CFG["protocol_limits"]
    if name not in limits:
        raise KeyError(
            f"No protocol limit '{name}'. Add it to the 'protocol_limits' "
            f"block in detection.yaml."
        )
    return int(limits[name])


def fraction_of_limit(fraction: Any, limit_name: str) -> int:
    """Byte threshold expressed as a fraction of a named protocol limit.

    Several scorers derive a byte threshold as ``fraction * protocol_limit(...)``
    (token_dust's value-CBOR floor, large_datum's size backstop). This names the
    ``int(fraction * limit)`` idiom so the derivation is not duplicated across
    scorers and reads as intent rather than arithmetic.
    """
    return int(float(fraction) * protocol_limit(limit_name))


def anchor(container: Dict[str, Any], key: str) -> Tuple[float, float]:
    """Extract ``(p50, p99)`` from a ``{key: {p50: ..., p99: ...}}`` mapping."""
    a = container[key]
    return float(a["p50"]), float(a["p99"])


def load_network_map(
    raw: Any,
    *,
    scorer: str,
    field: str,
    collect=tuple,
) -> Dict[str, Any]:
    """Normalise a ``{network: [str, ...]}`` config block into ``{network: collect([...])}``.

    Both ``multiple_sat.allowlist_prefixes`` and
    ``token_dust.allowlist_prefixes`` / ``allowlist_policies`` share the
    same shape and the same null-tolerant + fail-loud semantics. This
    helper centralises that logic so the two scorers cannot drift.

    - ``None`` and missing networks degrade to an empty collection at the
      call site (via ``.get(network, collect())``).
    - Non-mapping or non-list payloads raise a ``RuntimeError`` with the
      scorer and field name in the message, so a malformed YAML edit
      surfaces at import time rather than silently masking every alert.

    ``collect`` is the constructor used for each network's list of items;
    pass ``frozenset`` for membership-test callers (token_dust policies),
    leave as the default ``tuple`` for prefix-match callers.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"{scorer}.{field} must be a mapping of {{network: [...]}}; "
            f"got {type(raw).__name__}. Update config/detection.yaml."
        )
    out: Dict[str, Any] = {}
    for network, items in raw.items():
        if items is None:
            out[network] = collect()
            continue
        if not isinstance(items, list):
            raise RuntimeError(
                f"{scorer}.{field}.{network} must be a list; "
                f"got {type(items).__name__}."
            )
        out[network] = collect(items)
    return out


def resolved_or_bootstrap(
    feature: str,
    scope_type: str,
    scope_id: str,
    network: str,
    bootstrap: Dict[str, Any],
    bootstrap_key: str,
    scope_types_allowed: Optional[List[str]] = None,
) -> Tuple[float, float, str]:
    """Resolve a baseline, falling back to the scorer's configured bootstrap anchor.

    Wraps :func:`app.analysis.normalise.resolve_baseline` with the idiom every
    scorer repeats: if the resolved tier is ``"missing"``, replace
    ``(p50, p99)`` with the values from ``bootstrap[bootstrap_key]`` and report
    the source as ``"bootstrap"``.

    Parameters mirror ``resolve_baseline`` plus:
        bootstrap:            the scorer's ``bootstrap_anchors`` config sub-dict.
        bootstrap_key:        the key inside ``bootstrap`` to read when falling back.
        scope_types_allowed:  forwarded to ``resolve_baseline`` to restrict which
                              baseline tiers may be consulted. With
                              ``["per_script"]`` the global tier is skipped, so a
                              per-script miss drops straight to ``bootstrap``.

    Returns ``(p50, p99, source)`` where ``source`` is one of
    ``"per_script" | "per_policy" | "global" | "bootstrap"``.
    """
    p50, p99, source = resolve_baseline(
        feature, scope_type, scope_id, network,
        scope_types_allowed=scope_types_allowed,
    )
    if source == "missing":
        p50, p99 = anchor(bootstrap, bootstrap_key)
        source = "bootstrap"
    return p50, p99, source
