"""Loader for the engine's algorithm tunables (``config/clustering.yaml``).

Mirrors the host's ``backend/app/analysis/scorer_config.py`` pattern: the YAML
is loaded once at import time, a structural check requires every key the code
reads, unknown keys are rejected (a typo'd tunable must fail at import instead
of sitting silently unread while the code keeps a stale value), and cross-field
invariants are enforced with errors that name the offending YAML path.
Consumers read their section via :func:`get` and keep their module-level
constant names, so call sites are unchanged.
"""

from __future__ import annotations

import logging
import os
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Environment override for the config directory (mirrors the host's
# TMS_CONFIG_DIR); used by tests and non-standard deployments.
_CONFIG_DIR_ENV = "CLUSTERING_CONFIG_DIR"
_CONFIG_FILENAME = "clustering.yaml"

# Required keys per section: every tunable the code reads. A missing key fails
# at import with its dotted path. Every leaf in the document is a scalar or a
# list of scalars, so this mapping doubles as the exact allowlist for the
# unknown-key rejection below; the YAML and the code cannot drift.
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "evaluation": (
        "min_points",
        "max_curve_points",
        "fallback_eps",
        "knee_fallback_percentile",
        "precomputed_eps_grid",
        "eps_multipliers",
        "eps_percentiles",
        "eps_tail_clip_percentile",
        "max_dominant_cluster_ratio",
        "precomputed_min_samples",
        "min_samples_floor",
        "min_samples_ceil",
        "min_samples_grid_cap",
        "min_clusters",
        "max_noise_ratio",
    ),
    "anomaly": (
        "top_quantile",
        "lof_neighbors",
        "iso_estimators",
        "flag_vote_threshold",
        "fallback_eps_precomputed",
        "fallback_eps_euclidean",
    ),
    "explain": (
        "z_threshold",
        "band_far_z",
        "band_well_z",
        "top_k",
    ),
    "graph": ("svd_components",),
    "model": ("radius_quantile",),
}


def _config_dir() -> Path:
    """Resolve the config directory, honouring CLUSTERING_CONFIG_DIR if set.

    Without the override, walk upward from this file until a directory
    containing ``config/clustering.yaml`` is found: a repo checkout resolves to
    ``services/clustering/backend/config``, the Docker image to ``/app/config``.
    """
    override = os.environ.get(_CONFIG_DIR_ENV)
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "config" / _CONFIG_FILENAME
        if candidate.exists():
            return candidate.parent
    raise RuntimeError(
        f"Could not locate config/{_CONFIG_FILENAME} relative to {here}. "
        f"Set {_CONFIG_DIR_ENV} to override."
    )


def _missing_paths(data: dict[str, Any], path: Path) -> list[str]:
    """Dotted paths of required keys absent from ``data`` (sections included)."""
    missing: list[str] = []
    for section, keys in _REQUIRED_KEYS.items():
        block = data.get(section)
        if block is None:
            missing.append(section)
            continue
        if not isinstance(block, dict):
            raise RuntimeError(f"Clustering config {path}: '{section}' must be a mapping.")
        missing.extend(f"{section}.{key}" for key in keys if key not in block)
    return missing


def _unknown_paths(data: dict[str, Any]) -> list[str]:
    """Dotted paths of YAML keys outside the allowlist (typos, dead keys).

    The document is two levels deep by construction (validated leaves are
    scalars or lists), so a section walk covers every path.
    """
    unknown: list[str] = []
    for section, block in data.items():
        allowed = _REQUIRED_KEYS.get(str(section))
        if allowed is None:
            unknown.append(str(section))
            continue
        if isinstance(block, dict):
            unknown.extend(f"{section}.{key}" for key in block if key not in allowed)
    return unknown


def _require(check: bool, path: Path, message: str) -> None:
    """Raise a RuntimeError naming the config file when an invariant fails."""
    if not check:
        raise RuntimeError(f"Clustering config {path}: {message}")


def _strictly_ascending(values: list[Any]) -> bool:
    return all(float(a) < float(b) for a, b in pairwise(values))


def _check_invariants(path: Path, data: dict[str, Any]) -> None:
    """Cross-field sanity checks. Each failure names the yaml path so a bad
    edit is fixable from the error alone."""
    ev = data["evaluation"]
    _require(
        int(ev["min_points"]) >= 2,
        path,
        "evaluation.min_points must be >= 2 (DBSCAN needs at least a pair of points)",
    )
    _require(
        int(ev["max_curve_points"]) >= 2,
        path,
        "evaluation.max_curve_points must be >= 2 (a curve needs at least two plot points)",
    )
    _require(
        float(ev["fallback_eps"]) > 0,
        path,
        "evaluation.fallback_eps must be > 0 (eps is a positive radius)",
    )
    _require(
        0 < float(ev["knee_fallback_percentile"]) <= 100,
        path,
        "evaluation.knee_fallback_percentile must be in (0, 100]",
    )
    grid = ev["precomputed_eps_grid"]
    _require(
        isinstance(grid, list) and len(grid) > 0,
        path,
        "evaluation.precomputed_eps_grid must be a non-empty list",
    )
    _require(
        all(0 < float(v) <= 1 for v in grid),
        path,
        "evaluation.precomputed_eps_grid values must each be in (0, 1] "
        "(Jaccard distances live there)",
    )
    _require(
        _strictly_ascending(grid),
        path,
        "evaluation.precomputed_eps_grid must be strictly ascending",
    )
    mults = ev["eps_multipliers"]
    _require(
        isinstance(mults, list) and len(mults) > 0,
        path,
        "evaluation.eps_multipliers must be a non-empty list",
    )
    _require(
        all(float(v) > 0 for v in mults),
        path,
        "evaluation.eps_multipliers must all be positive",
    )
    _require(
        _strictly_ascending(mults),
        path,
        "evaluation.eps_multipliers must be strictly ascending",
    )
    pcts = ev["eps_percentiles"]
    _require(
        isinstance(pcts, list) and len(pcts) > 0,
        path,
        "evaluation.eps_percentiles must be a non-empty list",
    )
    _require(
        all(0 < float(v) < 100 for v in pcts),
        path,
        "evaluation.eps_percentiles values must each be in (0, 100) (percentiles)",
    )
    _require(
        _strictly_ascending(pcts),
        path,
        "evaluation.eps_percentiles must be strictly ascending",
    )
    _require(
        0 < float(ev["eps_tail_clip_percentile"]) <= 100,
        path,
        "evaluation.eps_tail_clip_percentile must be in (0, 100] (a percentile)",
    )
    _require(
        float(ev["eps_tail_clip_percentile"]) >= float(pcts[-1]),
        path,
        "evaluation.eps_tail_clip_percentile must be >= the largest eps_percentiles "
        "entry (else it would winsorise away the anchors it feeds)",
    )
    _require(
        int(ev["precomputed_min_samples"]) >= 2,
        path,
        "evaluation.precomputed_min_samples must be >= 2 "
        "(DBSCAN's smallest meaningful neighbourhood)",
    )
    _require(
        int(ev["min_samples_floor"])
        <= int(ev["min_samples_ceil"])
        <= int(ev["min_samples_grid_cap"]),
        path,
        "evaluation.min_samples_floor <= evaluation.min_samples_ceil "
        "<= evaluation.min_samples_grid_cap must hold",
    )
    _require(int(ev["min_clusters"]) >= 1, path, "evaluation.min_clusters must be >= 1")
    _require(
        0 < float(ev["max_noise_ratio"]) <= 1,
        path,
        "evaluation.max_noise_ratio must be in (0, 1] (a fraction of points)",
    )
    _require(
        0 < float(ev["max_dominant_cluster_ratio"]) <= 1,
        path,
        "evaluation.max_dominant_cluster_ratio must be in (0, 1] (a fraction of points)",
    )

    an = data["anomaly"]
    _require(
        0 < float(an["top_quantile"]) < 1,
        path,
        "anomaly.top_quantile must be in (0, 1)",
    )
    _require(
        int(an["lof_neighbors"]) >= 2,
        path,
        "anomaly.lof_neighbors must be >= 2 (LOF needs at least two neighbours)",
    )
    _require(int(an["iso_estimators"]) >= 1, path, "anomaly.iso_estimators must be >= 1")
    _require(
        1 <= int(an["flag_vote_threshold"]) <= 3,
        path,
        "anomaly.flag_vote_threshold must be in [1, 3] (three detectors vote)",
    )
    _require(
        0 < float(an["fallback_eps_precomputed"]) <= 1,
        path,
        "anomaly.fallback_eps_precomputed must be in (0, 1] (Jaccard distances live there)",
    )
    _require(
        float(an["fallback_eps_euclidean"]) > 0,
        path,
        "anomaly.fallback_eps_euclidean must be > 0",
    )

    ex = data["explain"]
    _require(
        float(ex["z_threshold"]) <= float(ex["band_well_z"]) < float(ex["band_far_z"]),
        path,
        "explain.z_threshold <= explain.band_well_z < explain.band_far_z must hold "
        "(the wording bands must be reachable and ordered)",
    )
    _require(int(ex["top_k"]) >= 1, path, "explain.top_k must be >= 1")

    _require(
        int(data["graph"]["svd_components"]) >= 1,
        path,
        "graph.svd_components must be >= 1",
    )
    _require(
        0 < float(data["model"]["radius_quantile"]) <= 1,
        path,
        "model.radius_quantile must be in (0, 1] (a quantile)",
    )


def _validate(path: Path, data: dict[str, Any]) -> None:
    if not isinstance(data, dict) or not data:
        raise RuntimeError(
            f"Clustering config {path} must contain the tunable section mappings "
            f"({', '.join(_REQUIRED_KEYS)})."
        )
    missing = _missing_paths(data, path)
    if missing:
        raise RuntimeError(
            f"Clustering config {path} missing required keys: {', '.join(sorted(missing))}"
        )
    unknown = _unknown_paths(data)
    if unknown:
        raise RuntimeError(
            f"Clustering config {path} contains unknown keys (typo or removed "
            f"tunable; every key must be read by the code): {', '.join(sorted(unknown))}"
        )
    _check_invariants(path, data)


def load_tunables(config_dir: Path | str | None = None) -> dict[str, Any]:
    """Load and validate the tunables document.

    ``config_dir`` overrides directory resolution (tests point it at a temp
    dir); the default resolves via CLUSTERING_CONFIG_DIR or the upward walk.
    """
    directory = Path(config_dir) if config_dir is not None else _config_dir()
    path = directory / _CONFIG_FILENAME
    if not path.exists():
        raise RuntimeError(f"Clustering config not found at {path}.")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _validate(path, data)
    logger.info(f"Clustering tunables loaded from {path.name}")
    return data


_CFG: dict[str, Any] = load_tunables()


def get(section: str) -> dict[str, Any]:
    """Return a tunables section (e.g. ``'evaluation'``)."""
    cfg = _CFG.get(section)
    if cfg is None:
        raise KeyError(
            f"No tunables section '{section}'. Add a '{section}' block to "
            f"config/{_CONFIG_FILENAME}."
        )
    return cfg
