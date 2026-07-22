"""Loader for client-tunable detection scorer configuration.

Loads ``config/detection.yaml`` at import time. The file is tracked in git:
edits to detection thresholds, weights, and allowlists are versioned and
reviewable. Scorers read their section via :func:`get`.

The config is intentionally a plain nested-dict structure so clients can edit
YAML without running a validation toolchain. A shallow structural check runs
at load time so a missing or misnamed key fails with an error that names the
file and the key path, not a deep ``KeyError`` from inside a scorer module.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from app.analysis.normalise import (
    BAND_CRITICAL_THRESHOLD,
    BAND_HIGH_THRESHOLD,
    BAND_MODERATE_THRESHOLD,
    resolve_baseline,
)
from app.config import settings

logger = logging.getLogger(__name__)

# Required top-level keys for each scorer section. Extend the set when a
# scorer starts reading a new block. Nested key validation is left to the
# scorer itself (KeyError there still beats a silent wrong value).
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "multiple_sat": (
        "weights",
        "bootstrap_anchors",
        "allowlist_prefixes",
        "reason_threshold",
        "lazy_validator_threshold",
        "lazy_validator_floor",
        "lazy_validator_extraction_min",
        "per_script_extraction_headroom",
        "uniform_sweep_guard.enabled",
        "uniform_sweep_guard.require_uniform_redeemer",
        "uniform_sweep_guard.require_no_script_return",
        "uniform_sweep_guard.min_inputs",
        "suppression_escape.enabled",
        "suppression_escape.extraction_floor_min",
        "saturation_floor.extraction_min",
        "saturation_floor.inputs_min",
        "saturation_floor.floor",
    ),
    "large_datum": (
        "gate",
        "gate.flag_datum_hash_only",
        "weights",
        "fixed_anchors",
        "bootstrap_anchors",
        "aggregate_engagement_min",
        "reason_threshold",
    ),
    "token_dust": (
        "gate.min_token_count",
        "weights",
        "bootstrap_anchors",
        "allowlist_prefixes",
        "allowlist_policies",
        "dos_asset_min",
        "established_collection.enabled",
        "established_collection.min_policy_age_slots",
        "reason_threshold",
    ),
    "large_value": ("weights", "bootstrap_anchors", "reason_threshold", "min_digits_subscore"),
    "front_running": (
        "weights",
        "fixed_anchors",
        "bootstrap_anchors",
        "outcome_scores",
        "reason_thresholds",
        "min_recurrence_wins",
        "high_band_cap",
        "delta_ms_default",
    ),
    "sandwich": (
        "weights",
        "fixed_anchors",
        "bootstrap_anchors",
        "link_scores",
        "window_slots",
        "neighbor_limit",
        "min_profit_lovelace",
        "reason_thresholds",
    ),
    "circular": (
        "weights",
        "fixed_anchors",
        "bootstrap_anchors",
        "cycle",
        "reason_threshold",
        "moderate_cap",
    ),
    "fake_token": (
        "weights",
        "fixed_anchors",
        "bootstrap_anchors",
        "similarity_threshold",
        "unicode_scores",
        "reason_thresholds",
        "critical_assets.multiplier",
        "critical_assets.names",
        "ascii_homoglyphs_enabled",
    ),
    "phishing": (
        "weights",
        "fixed_anchors",
        "bootstrap_anchors",
        "similarity_suspicious_range",
        "social_engineering",
        "reason_thresholds",
        "critical_threshold",
        "metadata_labels",
        "asset_name_carrier.enabled",
        "min_decoded_string_len",
    ),
}

# Weight and anchor names each scorer's code reads (the ``weights[...]`` and
# ``anchor(_BOOT/_FIXED, ...)`` call sites in
# backend/app/analysis/scorers/<name>.py). WHY this lives here as data
# instead of being derived from the scorer modules: scorers import this
# module at import time, so importing them back for introspection would be
# circular. The failure mode being closed is a typo'd anchor or weight name
# surfacing as a KeyError at SCORING time, where engine.py catches per-tx
# scorer exceptions and moves on, i.e. silent per-transaction recall loss.
# Validating the YAML names against these sets turns that typo into an
# import-time failure. Keep in sync with the scorer call sites when adding
# an axis; tests/analysis/test_scorer_config.py cross-checks the
# multiple_sat entries against the scorer's declared baseline specs.
_SCORER_WEIGHT_NAMES: dict[str, tuple[str, ...]] = {
    "multiple_sat": ("extraction", "exunits_inv", "inputs", "recurrence"),
    "large_datum": ("datum_bytes", "datum_ratio", "value_cbor_inv", "recurrence"),
    "token_dust": ("bytes", "assets", "ada_inv", "recurrence"),
    "large_value": ("digits", "bytes", "ada_inv", "recurrence"),
    "front_running": ("outcome", "delta", "recurrence", "structure"),
    "sandwich": ("link", "rate", "impact", "profit", "recurrence"),
    "circular": ("amount", "recurrence", "entropy", "auxiliary", "speed"),
    "fake_token": (
        "identity.name",
        "identity.unicode",
        "identity.cip25",
        "distribution.recipients",
        "distribution.ratio",
        "distribution.policy_age",
        "distribution.recurrence",
        "overall.identity",
        "overall.distribution",
    ),
    "phishing": (
        "content.blacklist",
        "content.domain",
        "content.social",
        "delivery.recipients",
        "delivery.url_recur",
        "delivery.targeting",
        "delivery.recurrence",
        "overall.content",
        "overall.delivery",
    ),
}

_SCORER_BOOTSTRAP_ANCHOR_NAMES: dict[str, tuple[str, ...]] = {
    "multiple_sat": (
        "net_value_out_of_script",
        "n_assets_out_of_script",
        "exunits_per_script_input",
        "n_inputs_same_script",
        "sender_recurrence",
    ),
    "large_datum": ("datum_bytes", "value_cbor_bytes"),
    "token_dust": ("value_cbor_bytes", "unique_token_count", "ada_amount"),
    "large_value": ("quantity_digits", "value_cbor_bytes", "ada_amount"),
    "front_running": ("attacker_recurrence",),
    "sandwich": ("price_impact", "swap_profit", "attacker_recurrence"),
    "circular": ("attacker_recurrence",),
    "fake_token": ("recipient_count", "mint_to_recipient_ratio"),
    "phishing": ("recipient_count",),
}

_SCORER_FIXED_ANCHOR_NAMES: dict[str, tuple[str, ...]] = {
    "multiple_sat": (),
    "large_datum": ("datum_ratio",),
    "token_dust": (),
    "large_value": (),
    "front_running": ("mempool_delta_inv", "fee_delta", "ttl_delta"),
    "sandwich": ("rate_delta",),
    "circular": ("amount_sim", "entropy", "hop_delta_inv", "temporal"),
    "fake_token": ("name_sim", "unicode", "cip25", "policy_age_inv"),
    "phishing": ("brand_sim", "social_score"),
}

# Anchor names allowed in the YAML but not (yet) consumed by code: declared
# for a documented-but-deferred axis, kept so the client-facing config does
# not have to churn when the axis lands. Allowed but never required.
_SCORER_OPTIONAL_FIXED_ANCHOR_NAMES: dict[str, tuple[str, ...]] = {
    # The phishing domain-age axis is documented in the detection spec and
    # its anchors are declared in the shipped config, but the signal needs
    # WHOIS enrichment, which is deferred.
    "phishing": ("domain_age_inv",),
}

# Dotted leaves the code reads (via .get(...) or hard [...] lookups in the
# scorers and their helper modules) that are not in _REQUIRED_KEYS. Together
# with _REQUIRED_KEYS and the weight/anchor name sets above, these form the
# allowlist for unknown-key rejection: any YAML key outside the allowlist
# fails at import, so a misspelled tunable cannot sit silently unread while
# the code keeps using a default or an old value.
_KNOWN_OPTIONAL_KEYS: dict[str, tuple[str, ...]] = {
    "multiple_sat": (),
    "large_datum": (
        "gate.min_datum_bytes",
        "gate.bloat_entropy_max",
        "gate.leaf_concentration_max",
        "gate.size_backstop_fraction",
    ),
    "token_dust": ("dos_value_cbor_fraction",),
    "large_value": (),
    "front_running": (
        "reason_thresholds.outcome",
        "reason_thresholds.delta",
        "reason_thresholds.recurrence",
        "unknown_outcome_score",
    ),
    "sandwich": (
        "link_scores.linked",
        "link_scores.unlinked",
        "reason_thresholds.link",
        "reason_thresholds.rate",
        "reason_thresholds.impact",
    ),
    "circular": (
        "structural_corroboration_floor",
        "recurrence_window_days",
        "cycle.min_length",
        "cycle.max_length",
        "cycle.fee_tolerance_multiplier",
        "cycle.fee_tolerance_strict",
        "cycle.per_hop_fee_estimate",
        "cycle.max_age_slots",
        "cycle.max_output_fanout",
        "cycle.bfs_hop_row_limit",
        "cycle.default_inter_hop_delta_slots",
    ),
    "fake_token": (
        "unicode_scores.zero_width",
        "unicode_scores.mixed_scripts",
        "unicode_scores.homoglyphs",
        "reason_thresholds.name",
        "reason_thresholds.unicode",
        "reason_thresholds.recipients",
    ),
    "phishing": (
        "similarity_suspicious_range.lo",
        "similarity_suspicious_range.hi",
        "social_engineering.urgency_increment",
        "social_engineering.urgency_cap",
        "social_engineering.brand_increment",
        "social_engineering.brand_cap",
        "social_engineering.url_combo_bonus",
        "social_engineering.phishing_tld_bonus",
        "asset_name_carrier.require_delivery_for_bonuses",
        "reason_thresholds.blacklist",
        "reason_thresholds.domain",
        "reason_thresholds.social",
        "reason_thresholds.recipients",
    ),
}

# Subtrees whose leaf names are operational data, not schema, so the
# unknown-key walk does not descend into them: network-keyed allowlists
# (shape-validated by load_network_map at scorer import) and the
# front_running outcome map (keys are collision-outcome labels produced by
# mempool ingestion, an open set this module must not have to mirror).
_FREEFORM_SUBTREES: frozenset = frozenset(
    {
        "scorers.multiple_sat.allowlist_prefixes",
        "scorers.token_dust.allowlist_prefixes",
        "scorers.token_dust.allowlist_policies",
        "scorers.front_running.outcome_scores",
    }
)


def _config_dir() -> Path:
    """Resolve the config directory, honouring TMS_CONFIG_DIR if set.

    TMS_CONFIG_DIR can come from .env (via pydantic settings) or the shell
    environment; shell wins when both are present.
    """
    override = os.environ.get("TMS_CONFIG_DIR") or settings.TMS_CONFIG_DIR or None
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
_REQUIRED_PROTOCOL_LIMITS: tuple[str, ...] = (
    "max_value_size_bytes",
    "max_tx_size_bytes",
)

# Required keys for the top-level composite_corroboration block (cross-class
# agreement signal; see detection.yaml). Top-level, not a scorer section.
_REQUIRED_COMPOSITE_CORROBORATION: tuple[str, ...] = ("corroboration_threshold",)

# Required keys for the top-level contract_anomaly block (projection of the
# clustering sidecar's verdict onto the host score; see detection.yaml). A
# top-level projection block, not a scorer section, so it has no weights or
# anchors. Dotted leaves so a missing floor fails fast with its full path.
_REQUIRED_CONTRACT_ANOMALY: tuple[str, ...] = (
    "verdict_floors.malicious",
    "verdict_floors.anomaly",
    "verdict_floors.benign",
    "verdict_floors.normal",
    "consensus_scale",
    "anomaly_consensus_scale",
    "corroboration_threshold",
    "freshness_seconds",
    "heartbeat_stale_seconds",
)

# Dotted leaves so a missing nested tunable fails fast with its full path
# at load time instead of a raw KeyError at first use.
_REQUIRED_BASELINES: tuple[str, ...] = (
    "min_spread_ratio",
    "per_script_p99_cap_multiplier",
    "per_script_p50_cap_spread_fraction",
    "drift.enabled",
    "drift.p99_threshold",
    "drift.p50_threshold",
    "windows.global_days",
    "windows.per_script_days",
)


def _missing_dotted(
    container: dict[str, Any],
    keys: tuple[str, ...],
    prefix: str,
) -> list[str]:
    """Return the full paths of dotted ``keys`` absent from ``container``.

    Dotted keys ("a.b.c") walk into nested dicts so callers can require
    leaf tunables, not just top-level blocks; the full path in the error
    lets YAML edits surface the precise missing field rather than a
    downstream KeyError at import.
    """
    missing: list[str] = []
    for key in keys:
        cur: Any = container
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                missing.append(f"{prefix}.{key}")
                break
            cur = cur[part]
    return missing


def _add_dotted(allowed: set, prefix: str, dotted: str) -> None:
    """Add ``prefix.dotted`` to ``allowed``, including every intermediate
    prefix, so the unknown-key walk can descend through nested blocks."""
    path = prefix
    for part in dotted.split("."):
        path = f"{path}.{part}" if path else part
        allowed.add(path)


def _allowed_paths() -> set:
    """Dotted paths of every key the code knows how to read.

    Derived from _REQUIRED_KEYS, _KNOWN_OPTIONAL_KEYS, and the per-scorer
    weight/anchor name sets, so the allowlist cannot drift from the
    validation that uses it. Anchor names additionally allow their
    ``p50`` / ``p99`` children (the shape ``anchor()`` reads).
    """
    allowed: set = {
        "protocol_limits",
        "composite_corroboration",
        "contract_anomaly",
        "baselines",
        "scorers",
    }
    for key in _REQUIRED_PROTOCOL_LIMITS:
        _add_dotted(allowed, "protocol_limits", key)
    for key in _REQUIRED_COMPOSITE_CORROBORATION:
        _add_dotted(allowed, "composite_corroboration", key)
    for key in _REQUIRED_CONTRACT_ANOMALY:
        _add_dotted(allowed, "contract_anomaly", key)
    for key in _REQUIRED_BASELINES:
        _add_dotted(allowed, "baselines", key)
    for scorer, keys in _REQUIRED_KEYS.items():
        prefix = f"scorers.{scorer}"
        allowed.add(prefix)
        for key in keys:
            _add_dotted(allowed, prefix, key)
        for key in _KNOWN_OPTIONAL_KEYS.get(scorer, ()):
            _add_dotted(allowed, prefix, key)
        for name in _SCORER_WEIGHT_NAMES.get(scorer, ()):
            _add_dotted(allowed, f"{prefix}.weights", name)
        anchor_groups = (
            ("bootstrap_anchors", _SCORER_BOOTSTRAP_ANCHOR_NAMES.get(scorer, ())),
            ("fixed_anchors", _SCORER_FIXED_ANCHOR_NAMES.get(scorer, ())),
            ("fixed_anchors", _SCORER_OPTIONAL_FIXED_ANCHOR_NAMES.get(scorer, ())),
        )
        for block, names in anchor_groups:
            for name in names:
                _add_dotted(allowed, f"{prefix}.{block}", f"{name}.p50")
                _add_dotted(allowed, f"{prefix}.{block}", f"{name}.p99")
    return allowed


def _unknown_paths(data: dict[str, Any]) -> list[str]:
    """Dotted paths of YAML keys outside the allowlist (typos, dead keys)."""
    allowed = _allowed_paths()
    unknown: list[str] = []

    def _walk(node: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if path not in allowed:
                unknown.append(path)
                continue
            if isinstance(value, dict) and path not in _FREEFORM_SUBTREES:
                _walk(value, path)

    _walk(data, "")
    return unknown


def _missing_scorer_names(scorer: str, section: dict[str, Any]) -> list[str]:
    """Required weight/anchor names absent from a scorer's YAML section.

    A missing name would otherwise surface as a KeyError at scoring time,
    where the engine swallows per-tx scorer exceptions (silent recall loss);
    here it fails at import with the full dotted path.
    """
    missing: list[str] = []
    blocks = (
        ("weights", _SCORER_WEIGHT_NAMES.get(scorer, ())),
        (
            "bootstrap_anchors",
            tuple(
                f"{name}.{leaf}"
                for name in _SCORER_BOOTSTRAP_ANCHOR_NAMES.get(scorer, ())
                for leaf in ("p50", "p99")
            ),
        ),
        (
            "fixed_anchors",
            tuple(
                f"{name}.{leaf}"
                for name in _SCORER_FIXED_ANCHOR_NAMES.get(scorer, ())
                for leaf in ("p50", "p99")
            ),
        ),
    )
    for block, names in blocks:
        if not names:
            continue
        container = section.get(block)
        if not isinstance(container, dict):
            # Block presence itself is reported by the _REQUIRED_KEYS pass.
            continue
        missing.extend(_missing_dotted(container, names, f"scorers.{scorer}.{block}"))
    return missing


def _validate(path: Path, data: dict[str, Any]) -> None:
    if "scorers" not in data or not isinstance(data["scorers"], dict):
        raise RuntimeError(f"Detection config {path} must contain a top-level 'scorers' mapping.")
    limits = data.get("protocol_limits")
    if not isinstance(limits, dict):
        raise RuntimeError(
            f"Detection config {path} must contain a top-level 'protocol_limits' mapping."
        )
    missing_limits = [k for k in _REQUIRED_PROTOCOL_LIMITS if k not in limits]
    if missing_limits:
        raise RuntimeError(
            f"Detection config {path} missing protocol_limits keys: {', '.join(missing_limits)}"
        )
    corroboration = data.get("composite_corroboration")
    if not isinstance(corroboration, dict):
        raise RuntimeError(
            f"Detection config {path} must contain a top-level 'composite_corroboration' mapping."
        )
    missing_corr = [k for k in _REQUIRED_COMPOSITE_CORROBORATION if k not in corroboration]
    if missing_corr:
        raise RuntimeError(
            f"Detection config {path} missing composite_corroboration keys: "
            f"{', '.join(missing_corr)}"
        )
    contract_anomaly = data.get("contract_anomaly")
    if not isinstance(contract_anomaly, dict):
        raise RuntimeError(
            f"Detection config {path} must contain a top-level 'contract_anomaly' mapping."
        )
    missing_ca = _missing_dotted(
        contract_anomaly,
        _REQUIRED_CONTRACT_ANOMALY,
        "contract_anomaly",
    )
    if missing_ca:
        raise RuntimeError(
            f"Detection config {path} missing contract_anomaly keys: {', '.join(missing_ca)}"
        )
    baselines = data.get("baselines")
    if not isinstance(baselines, dict):
        raise RuntimeError(f"Detection config {path} must contain a top-level 'baselines' mapping.")
    missing_bl = _missing_dotted(baselines, _REQUIRED_BASELINES, "baselines")
    if missing_bl:
        raise RuntimeError(
            f"Detection config {path} missing baselines keys: {', '.join(missing_bl)}"
        )
    scorers = data["scorers"]
    missing: list[str] = []
    for name, keys in _REQUIRED_KEYS.items():
        section = scorers.get(name)
        if section is None:
            missing.append(f"scorers.{name}")
            continue
        if not isinstance(section, dict):
            raise RuntimeError(f"Detection config {path}: scorers.{name} must be a mapping.")
        missing.extend(_missing_dotted(section, keys, f"scorers.{name}"))
        missing.extend(_missing_scorer_names(name, section))
    if missing:
        joined = ", ".join(sorted(missing))
        raise RuntimeError(f"Detection config {path} missing required keys: {joined}")
    unknown = _unknown_paths(data)
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise RuntimeError(
            f"Detection config {path} contains unknown keys (typo or removed "
            f"tunable; every key must be read by the code or listed as a "
            f"known optional): {joined}"
        )
    _check_band_invariants(path, data)


# Band-boundary contracts for config values that cap or floor a score into a
# specific band. Enforced here, at load time, so a detection.yaml edit that
# lands a cap/floor in the wrong band fails loud from one place instead of
# each scorer hand-rolling an import-time guard (and a future banded scorer
# adds a row here rather than remembering to copy a raise block). Entries:
# (dotted path from the document root, inclusive lower bound, exclusive
# upper bound or None, why the boundary exists).
_BAND_INVARIANTS: tuple[tuple[str, float, float | None, str], ...] = (
    (
        "scorers.multiple_sat.lazy_validator_floor",
        BAND_HIGH_THRESHOLD,
        None,
        "the lazy-validator promotion must reach the High band",
    ),
    (
        "scorers.multiple_sat.saturation_floor.floor",
        BAND_HIGH_THRESHOLD,
        None,
        "the saturated extraction+inputs promotion must reach the High band",
    ),
    (
        "scorers.front_running.high_band_cap",
        BAND_HIGH_THRESHOLD,
        BAND_CRITICAL_THRESHOLD,
        "the low-recurrence demotion must land inside the High band",
    ),
    (
        "scorers.circular.moderate_cap",
        BAND_MODERATE_THRESHOLD,
        BAND_HIGH_THRESHOLD,
        "the weak-corroboration demotion must land inside the Moderate band",
    ),
    (
        "contract_anomaly.verdict_floors.malicious",
        BAND_CRITICAL_THRESHOLD,
        None,
        "a curated-malicious verdict must band Critical",
    ),
    (
        "contract_anomaly.anomaly_consensus_scale",
        BAND_MODERATE_THRESHOLD,
        BAND_HIGH_THRESHOLD,
        "a bare auto-anomaly (consensus <= 1) must band Moderate at most: "
        "an unsupervised shape-outlier has no exploit semantics and must not "
        "page on its own (only a malicious LABEL or a stored detector alerts)",
    ),
)


def _dotted_get(data: dict[str, Any], dotted: str) -> Any:
    """Resolve a dotted path against nested dicts; None when any hop is absent."""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _check_band_invariants(path: Path, data: dict[str, Any]) -> None:
    for dotted, lower, upper, why in _BAND_INVARIANTS:
        raw = _dotted_get(data, dotted)
        if raw is None:
            # Presence is enforced by the required-key checks above (or, for
            # verdict_floors, by contract_anomaly.py's verdict/floor sync
            # guard); this check owns only the numeric band boundary.
            continue
        value = float(raw)
        if value < lower or (upper is not None and value >= upper):
            band = f"[{lower}, {upper})" if upper is not None else f">= {lower}"
            raise RuntimeError(
                f"Detection config {path}: {dotted}={value} violates its band "
                f"contract ({band}): {why}. Fix the value in detection.yaml "
                f"or the band thresholds in normalise.py."
            )


def _load() -> dict[str, Any]:
    path = _config_dir() / "detection.yaml"
    if not path.exists():
        raise RuntimeError(f"Detection config not found at {path}.")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _validate(path, data)
    logger.info(f"Detection config loaded from {path.name}")
    return data


_CFG: dict[str, Any] = _load()


def get(section: str) -> dict[str, Any]:
    """Return the config section for a given scorer (e.g. ``'multiple_sat'``)."""
    cfg = _CFG["scorers"].get(section)
    if cfg is None:
        raise KeyError(
            f"No config section for scorer '{section}'. "
            f"Add a 'scorers.{section}' block to detection.yaml."
        )
    return cfg


def baselines_config() -> dict[str, Any]:
    """Return the top-level baselines block (resolution tunables shared by
    every percentile-baselined scorer). Presence and required keys are
    enforced at load time by :func:`_validate`.
    """
    return _CFG["baselines"]


# Cap on a LEARNED baseline's p99 relative to the scorer's bootstrap anchor
# (baselines.per_script_p99_cap_multiplier; 0 disables). Applied in
# resolved_or_bootstrap; see its docstring for the poisoning threat model.
_P99_CAP_MULTIPLIER: float = float(_CFG["baselines"]["per_script_p99_cap_multiplier"])

# Minimum (p99 - p50) / p50 spread for a usable baseline; reused here to
# keep the p50 bound strictly below the p99 cap so a capped baseline can
# never collapse to a degenerate (p50 >= p99) pair.
_MIN_SPREAD_RATIO: float = float(_CFG["baselines"]["min_spread_ratio"])

# Anchor-relative bound on a LEARNED baseline's p50
# (baselines.per_script_p50_cap_spread_fraction): resolved p50 is clamped to
# anchor_p50 + K * (anchor_p99 - anchor_p50). Bounding p50 relative to the
# CAP instead (the previous behaviour, ~4.55x the anchor p99) left enough
# room for an in-bound median-poisoned pair to zero a real drain below the
# suppression-escape floor; see the derivation in config/detection.yaml.
_P50_CAP_SPREAD_FRACTION: float = float(_CFG["baselines"]["per_script_p50_cap_spread_fraction"])


def composite_corroboration_config() -> dict[str, Any]:
    """Return the top-level composite_corroboration block.

    Cross-class agreement signal (not a scorer section). Presence and required
    keys are enforced at load time by :func:`_validate`.
    """
    return _CFG["composite_corroboration"]


def contract_anomaly_config() -> dict[str, Any]:
    """Return the top-level contract_anomaly projection block.

    Maps the clustering sidecar's verdict (consensus, votes, verdict label)
    onto the host 0-100 score + risk band (not a scorer section). Presence and
    required keys are enforced at load time by :func:`_validate`. Shared by the
    sidecar (write-time score mapping) and the host API (corroboration /
    staleness thresholds) so the two cannot disagree.
    """
    return _CFG["contract_anomaly"]


def protocol_limit(name: str) -> int:
    """Return a Cardano ledger protocol limit (e.g. ``'max_tx_size_bytes'``).

    These are top-level (not per-scorer) so multiple scorers derive byte
    thresholds from the same named value instead of repeating raw constants.
    Presence is enforced at load time by :func:`_validate`.
    """
    limits = _CFG["protocol_limits"]
    if name not in limits:
        raise KeyError(
            f"No protocol limit '{name}'. Add it to the 'protocol_limits' block in detection.yaml."
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


def anchor(container: dict[str, Any], key: str) -> tuple[float, float]:
    """Extract ``(p50, p99)`` from a ``{key: {p50: ..., p99: ...}}`` mapping."""
    a = container[key]
    return float(a["p50"]), float(a["p99"])


def load_network_map(
    raw: Any,
    *,
    scorer: str,
    field: str,
    collect=tuple,
) -> dict[str, Any]:
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
    out: dict[str, Any] = {}
    for network, items in raw.items():
        if items is None:
            out[network] = collect()
            continue
        if not isinstance(items, list):
            raise RuntimeError(
                f"{scorer}.{field}.{network} must be a list; got {type(items).__name__}."
            )
        out[network] = collect(items)
    return out


def resolved_or_bootstrap(
    feature: str,
    scope_type: str,
    scope_id: str,
    network: str,
    bootstrap: dict[str, Any],
    bootstrap_key: str,
    scope_types_allowed: list[str] | None = None,
) -> tuple[float, float, str]:
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

    A learned baseline's p99 is additionally CAPPED at
    ``baselines.per_script_p99_cap_multiplier`` times the scorer's bootstrap
    anchor p99. The normalisation saturation point is the resolved p99, so an
    attacker who pre-trains a wide per-script distribution (~200 outputs over
    the 90-day window, roughly tens of ADA in fees) could otherwise push p99
    arbitrarily high and have a real attack normalise to ~0. The cap bounds
    that de-sensitisation: an established contract may legitimately run up
    to K times the protocol-grounded anchor, but never so far that the
    anchor's threat model becomes unreachable.

    The p50 is bounded too: ``normalise(value, p50, p99)`` subtracts p50
    first, so drifting the MEDIAN upward de-sensitises an axis exactly like
    widening the tail (values below the learned median score 0), and the
    p99 cap alone could even create a degenerate p50 >= p99 pair from a
    median-poisoned baseline. The bound is ANCHOR-relative,
    ``anchor_p50 + K * (anchor_p99 - anchor_p50)`` with
    ``K = baselines.per_script_p50_cap_spread_fraction``: a cap-relative
    bound left an in-bound median-poisoned pair enough room to zero a real
    drain below the suppression-escape floor (see the config derivation).
    An anchor p50 of 0 (count-like features) degrades naturally to
    ``K * anchor_p99``, and the bound is additionally kept below the p99
    cap via min_spread_ratio so the capped pair always keeps a usable
    spread.

    Returns ``(p50, p99, source)`` where ``source`` is one of
    ``"per_script" | "per_policy" | "global" | "bootstrap"``.
    """
    p50, p99, source = resolve_baseline(
        feature,
        scope_type,
        scope_id,
        network,
        scope_types_allowed=scope_types_allowed,
    )
    if source == "missing":
        p50, p99 = anchor(bootstrap, bootstrap_key)
        source = "bootstrap"
    elif _P99_CAP_MULTIPLIER > 0:
        anchor_p50, anchor_p99 = anchor(bootstrap, bootstrap_key)
        if anchor_p99 > 0:
            cap = _P99_CAP_MULTIPLIER * anchor_p99
            p99 = min(p99, cap)
            # min() of both bounds: the anchor-relative term carries the
            # escape-floor guarantee; the cap-relative term (the previous
            # sole bound) guards a misconfigured K from ever producing a
            # degenerate p50 >= capped-p99 pair. Taking the min also means
            # this change can only ever LOWER a resolved p50 versus the
            # previous behaviour (recall-positive tightening).
            p50_bound = min(
                anchor_p50 + _P50_CAP_SPREAD_FRACTION * (anchor_p99 - anchor_p50),
                cap / (1.0 + _MIN_SPREAD_RATIO),
            )
            p50 = min(p50, p50_bound)
    return p50, p99, source
