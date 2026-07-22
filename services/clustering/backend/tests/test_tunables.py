"""Golden locks + validation tests for the config/clustering.yaml loader.

The golden values below are deliberately HARDCODED copies of the literals that
lived in the code before the tunables moved to YAML (recall-sensitive: the move
must not change a single value). If a test here fails after an intentional
retune, update the golden alongside the YAML in the same reviewed change.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from app import tunables
from app.clustering.evaluate import evaluate
from app.features import ClusteringInput
from app.tunables import load_tunables

# Every tunable, valued exactly as the pre-YAML module literal it replaced.
_GOLDEN: dict[str, dict[str, Any]] = {
    "evaluation": {
        "min_points": 3,
        "max_curve_points": 1500,
        "fallback_eps": 0.5,
        "knee_fallback_percentile": 90,
        "precomputed_eps_grid": [0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        "eps_multipliers": [0.5, 0.75, 1.0, 1.25, 1.5, 2.0],
        "eps_percentiles": [50, 65, 80, 90, 95],
        "eps_tail_clip_percentile": 97.5,
        "max_dominant_cluster_ratio": 0.9,
        "precomputed_min_samples": 4,
        "min_samples_floor": 4,
        "min_samples_ceil": 24,
        "min_samples_grid_cap": 32,
        "min_clusters": 2,
        "max_noise_ratio": 0.9,
    },
    "anomaly": {
        "top_quantile": 0.05,
        "lof_neighbors": 20,
        "iso_estimators": 300,
        "flag_vote_threshold": 2,
        "fallback_eps_precomputed": 0.5,
        "fallback_eps_euclidean": 1.0,
    },
    "explain": {
        "z_threshold": 2.0,
        "band_far_z": 4.0,
        "band_well_z": 2.75,
        "top_k": 3,
    },
    "graph": {"svd_components": 8},
    "model": {"radius_quantile": 0.95},
}


# --- (a) golden lock ---------------------------------------------------------


def test_golden_lock_every_section_matches_the_previous_literals() -> None:
    for section, expected in _GOLDEN.items():
        assert tunables.get(section) == expected, f"section '{section}' drifted"


def test_golden_lock_consumer_constants_keep_their_values() -> None:
    """The loader wiring must leave the module-level constant names bound to the
    exact pre-YAML values (the names are imported cross-module)."""
    import importlib

    from app.anomaly import detect
    from app.clustering import model
    from app.features import explain, graph

    # The package re-exports the evaluate() function under the module's name,
    # so the module itself must come from importlib.
    ev = importlib.import_module("app.clustering.evaluate")

    assert ev.MIN_POINTS == 3
    assert ev.FALLBACK_EPS == 0.5
    assert ev.MIN_SAMPLES_FLOOR == 4
    assert detect.DEFAULT_TOP_QUANTILE == 0.05
    assert detect.LOF_NEIGHBORS == 20
    assert detect.ISO_ESTIMATORS == 300
    assert detect.FLAG_VOTE_THRESHOLD == 2
    assert explain._Z_THRESHOLD == 2.0
    assert explain._BAND_FAR_Z == 4.0
    assert explain._BAND_WELL_Z == 2.75
    assert graph._SVD_COMPONENTS == 8
    assert model._RADIUS_QUANTILE == 0.95


# --- (b) structural validation -----------------------------------------------


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    (tmp_path / "clustering.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return tmp_path


def test_unknown_key_rejected_naming_the_path(tmp_path: Path) -> None:
    data = copy.deepcopy(_GOLDEN)
    data["evaluation"]["min_pointz"] = 3  # typo'd tunable must fail at load
    with pytest.raises(RuntimeError, match=r"evaluation\.min_pointz"):
        load_tunables(_write(tmp_path, data))


def test_unknown_section_rejected_naming_the_path(tmp_path: Path) -> None:
    data = copy.deepcopy(_GOLDEN)
    data["extras"] = {"anything": 1}
    with pytest.raises(RuntimeError, match=r"unknown keys.*extras"):
        load_tunables(_write(tmp_path, data))


def test_missing_key_rejected_naming_the_path(tmp_path: Path) -> None:
    data = copy.deepcopy(_GOLDEN)
    del data["model"]["radius_quantile"]
    with pytest.raises(RuntimeError, match=r"missing required keys.*model\.radius_quantile"):
        load_tunables(_write(tmp_path, data))


def test_missing_config_file_raises_with_path(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not found"):
        load_tunables(tmp_path)


def test_valid_golden_document_loads(tmp_path: Path) -> None:
    assert load_tunables(_write(tmp_path, copy.deepcopy(_GOLDEN))) == _GOLDEN


# --- (c) invariant violations --------------------------------------------------


@pytest.mark.parametrize(
    ("section", "key", "bad_value", "message_fragment"),
    [
        ("evaluation", "min_points", 1, r"evaluation\.min_points"),
        ("evaluation", "fallback_eps", 0, r"evaluation\.fallback_eps"),
        ("evaluation", "max_noise_ratio", 1.5, r"evaluation\.max_noise_ratio"),
        # Not ascending.
        ("evaluation", "precomputed_eps_grid", [0.3, 0.2], r"evaluation\.precomputed_eps_grid"),
        # Out of the (0, 1] Jaccard range.
        ("evaluation", "precomputed_eps_grid", [0.5, 1.2], r"evaluation\.precomputed_eps_grid"),
        ("evaluation", "eps_multipliers", [-0.5, 1.0], r"evaluation\.eps_multipliers"),
        # eps_percentiles: not ascending / out of the (0, 100) range.
        ("evaluation", "eps_percentiles", [80, 50], r"evaluation\.eps_percentiles"),
        ("evaluation", "eps_percentiles", [50, 120], r"evaluation\.eps_percentiles"),
        # eps_tail_clip_percentile out of (0, 100] / below the largest percentile (95).
        ("evaluation", "eps_tail_clip_percentile", 150, r"evaluation\.eps_tail_clip_percentile"),
        ("evaluation", "eps_tail_clip_percentile", 90, r"evaluation\.eps_tail_clip_percentile"),
        # max_dominant_cluster_ratio out of (0, 1].
        ("evaluation", "max_dominant_cluster_ratio", 0, r"evaluation\.max_dominant_cluster_ratio"),
        (
            "evaluation",
            "max_dominant_cluster_ratio",
            1.5,
            r"evaluation\.max_dominant_cluster_ratio",
        ),
        # Breaks floor <= ceil <= grid_cap (floor stays 4).
        ("evaluation", "min_samples_ceil", 3, r"evaluation\.min_samples_floor"),
        ("evaluation", "min_samples_grid_cap", 10, r"evaluation\.min_samples_ceil"),
        ("anomaly", "top_quantile", 1.0, r"anomaly\.top_quantile"),
        # Only three detectors vote; a threshold of 4 could never fire.
        ("anomaly", "flag_vote_threshold", 4, r"anomaly\.flag_vote_threshold"),
        ("anomaly", "flag_vote_threshold", 0, r"anomaly\.flag_vote_threshold"),
        ("anomaly", "fallback_eps_precomputed", 1.5, r"anomaly\.fallback_eps_precomputed"),
        # band_well_z above band_far_z breaks the wording-band ordering.
        ("explain", "band_well_z", 5.0, r"explain\.band_well_z"),
        # z_threshold above band_well_z makes the "well above" band unreachable.
        ("explain", "z_threshold", 3.0, r"explain\.z_threshold"),
        ("graph", "svd_components", 0, r"graph\.svd_components"),
        ("model", "radius_quantile", 0.0, r"model\.radius_quantile"),
    ],
)
def test_invariant_violation_raises_naming_the_path(
    tmp_path: Path, section: str, key: str, bad_value: Any, message_fragment: str
) -> None:
    data = copy.deepcopy(_GOLDEN)
    data[section][key] = bad_value
    with pytest.raises(RuntimeError, match=message_fragment):
        load_tunables(_write(tmp_path, data))


# --- (d) seeded golden run -----------------------------------------------------


def test_seeded_golden_run_recommendation_is_bit_identical() -> None:
    """Recall lock: moving the tunables into clustering.yaml must not move the
    recommended DBSCAN parameters. The expected pair was captured by running
    this exact fixed-rng input through evaluate() at the pre-change HEAD."""
    rng = np.random.default_rng(0)
    a = rng.normal(loc=0.0, scale=0.3, size=(30, 2))
    b = rng.normal(loc=10.0, scale=0.3, size=(30, 2))
    X = np.vstack([a, b])
    ci = ClusteringInput(
        [f"tx{i}" for i in range(X.shape[0])], X, "euclidean", "shape", ["f0", "f1"]
    )
    rec = evaluate(ci)["recommended"]
    assert rec is not None
    assert (rec["eps"], rec["min_samples"]) == (0.247694, 8)
