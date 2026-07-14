"""Tests for the detection config loader.

The minimal config fixture is built from the loader's own declared
requirements (_REQUIRED_KEYS, the weight/anchor name sets), so the tests
reference the validation constants rather than duplicating literals; a new
required key automatically appears in the fixture.
"""

import importlib
import os
from pathlib import Path

import pytest
import yaml


def _sc():
    import app.analysis.scorer_config as sc
    return sc


@pytest.fixture(scope="module", autouse=True)
def _restore_shipped_config():
    """Reload the shipped config after this module's reload games, so later
    test modules see real values in scorer_config's module globals."""
    yield
    os.environ.pop("TMS_CONFIG_DIR", None)
    importlib.reload(_sc())


def _reload_module(monkeypatch, tmp_path, cfg):
    """Write cfg (dict or raw YAML string) to a temp detection.yaml and
    reload scorer_config against it."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    body = cfg if isinstance(cfg, str) else yaml.safe_dump(cfg, sort_keys=False)
    (cfg_dir / "detection.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("TMS_CONFIG_DIR", str(cfg_dir))
    return importlib.reload(_sc())


def _reload_shipped(monkeypatch):
    """Reload scorer_config against the SHIPPED config/detection.yaml."""
    sc = _sc()
    repo_config = Path(sc.__file__).resolve().parents[3] / "config"
    monkeypatch.setenv("TMS_CONFIG_DIR", str(repo_config))
    return importlib.reload(sc)


def _expand_dotted(keys, leaf_value):
    """Build a nested dict from dotted keys, each leaf set to leaf_value.

    Handles overlapping keys (e.g. "gate" plus "gate.flag_datum_hash_only")
    in either order: a path that needs children always becomes a dict.
    """
    out = {}
    for dotted in keys:
        cur = out
        parts = dotted.split(".")
        for part in parts[:-1]:
            if not isinstance(cur.get(part), dict):
                cur[part] = {}
            cur = cur[part]
        if not isinstance(cur.get(parts[-1]), dict):
            cur[parts[-1]] = leaf_value
    return out


def _minimal_config():
    """Config dict satisfying every requirement the loader declares."""
    sc = _sc()
    cfg = {
        "protocol_limits": {k: 5000 for k in sc._REQUIRED_PROTOCOL_LIMITS},
        "composite_corroboration": {
            k: 40.0 for k in sc._REQUIRED_COMPOSITE_CORROBORATION
        },
        # Top-level contract_anomaly projection block, built from the loader's
        # own declared requirements so a new required key auto-appears here.
        "contract_anomaly": _expand_dotted(sc._REQUIRED_CONTRACT_ANOMALY, 40.0),
        # Values mirror the shipped config: tests that do NOT reload read
        # scorer_config module globals (e.g. _P99_CAP_MULTIPLIER), and a
        # reload from this fixture must not silently change them.
        "baselines": {
            "min_spread_ratio": 0.10,
            "per_script_p99_cap_multiplier": 5.0,
            "per_script_p50_cap_spread_fraction": 0.25,
            "drift": {
                "enabled": True,
                "p99_threshold": 0.50,
                "p50_threshold": 0.50,
            },
            "windows": {"global_days": 180, "per_script_days": 90},
        },
        "scorers": {},
    }
    for scorer, keys in sc._REQUIRED_KEYS.items():
        section = _expand_dotted(keys, 1)
        section["weights"] = _expand_dotted(
            sc._SCORER_WEIGHT_NAMES[scorer], 0.25,
        )
        section["bootstrap_anchors"] = {
            name: {"p50": 0, "p99": 1}
            for name in sc._SCORER_BOOTSTRAP_ANCHOR_NAMES[scorer]
        }
        if sc._SCORER_FIXED_ANCHOR_NAMES.get(scorer):
            section["fixed_anchors"] = {
                name: {"p50": 0, "p99": 1}
                for name in sc._SCORER_FIXED_ANCHOR_NAMES[scorer]
            }
        cfg["scorers"][scorer] = section
    # Band-constrained values (caps/floors that must land in a specific band)
    # are pinned to each invariant's inclusive lower bound, driven by the
    # loader's own _BAND_INVARIANTS table so the fixture stays valid by
    # construction when a new invariant row is added.
    for dotted, lower, _upper, _why in sc._BAND_INVARIANTS:
        _set_dotted(cfg, dotted, lower)
    return cfg


def _set_dotted(cfg, dotted, value):
    """Set a dotted path in nested dicts, creating intermediate mappings."""
    cur = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


class TestLoader:
    def test_loads_minimal_config(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_config())
        assert "multiple_sat" in sc._CFG["scorers"]

    def test_shipped_config_passes_validation(self, monkeypatch):
        # The real client-facing config/detection.yaml must satisfy the
        # full validation: required keys, weight/anchor names, no unknowns.
        sc = _reload_shipped(monkeypatch)
        assert "multiple_sat" in sc._CFG["scorers"]
        assert sc._P50_CAP_SPREAD_FRACTION > 0

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        monkeypatch.setenv("TMS_CONFIG_DIR", str(cfg_dir))
        with pytest.raises(RuntimeError, match="Detection config not found"):
            importlib.reload(_sc())

    def test_env_override_honoured(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_config())
        # The resolved config_dir must match our tmp_path.
        assert sc._config_dir() == tmp_path / "config"


class TestValidation:
    def test_missing_scorers_key_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="top-level 'scorers' mapping"):
            _reload_module(monkeypatch, tmp_path, "other_root: {}")

    def test_missing_scorer_section_raises_with_path(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        del cfg["scorers"]["multiple_sat"]
        with pytest.raises(RuntimeError, match="scorers.multiple_sat"):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_missing_required_key_raises_with_path(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        del cfg["scorers"]["multiple_sat"]["allowlist_prefixes"]
        with pytest.raises(RuntimeError, match="scorers.multiple_sat.allowlist_prefixes"):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_missing_nested_required_key_raises_with_full_path(self, tmp_path, monkeypatch):
        # The token_dust scorer requires gate.min_token_count. Removing only
        # the leaf field (while leaving the gate block in place) must surface
        # the dotted path, not a downstream KeyError at scorer import time.
        cfg = _minimal_config()
        del cfg["scorers"]["token_dust"]["gate"]["min_token_count"]
        with pytest.raises(RuntimeError, match="scorers.token_dust.gate.min_token_count"):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_missing_protocol_limits_raises(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        del cfg["protocol_limits"]
        with pytest.raises(RuntimeError, match="top-level 'protocol_limits' mapping"):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_non_dict_scorer_section_raises(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        cfg["scorers"]["multiple_sat"] = "not a mapping"
        with pytest.raises(RuntimeError, match="must be a mapping"):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_missing_contract_anomaly_raises(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        del cfg["contract_anomaly"]
        with pytest.raises(RuntimeError, match="top-level 'contract_anomaly' mapping"):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_missing_contract_anomaly_floor_raises_with_path(
        self, tmp_path, monkeypatch
    ):
        # A missing verdict floor must fail fast with its full dotted path, so a
        # mis-edited projection cannot silently score every verdict at 0.
        cfg = _minimal_config()
        del cfg["contract_anomaly"]["verdict_floors"]["malicious"]
        with pytest.raises(
            RuntimeError, match=r"contract_anomaly\.verdict_floors\.malicious",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_band_invariant_violation_raises(self, tmp_path, monkeypatch):
        # A cap that escapes its band contract must fail at load, not surface
        # as a silently wrong band at scoring time. Exercises the centralized
        # _BAND_INVARIANTS check that replaced the per-scorer import guards.
        cfg = _minimal_config()
        cfg["scorers"]["front_running"]["high_band_cap"] = 10.0
        with pytest.raises(
            RuntimeError, match=r"high_band_cap.*violates its band contract",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_band_floor_below_threshold_raises(self, tmp_path, monkeypatch):
        # Same contract for floors: a malicious verdict floor below the
        # Critical threshold would demote curated-malicious verdicts.
        cfg = _minimal_config()
        cfg["contract_anomaly"]["verdict_floors"]["malicious"] = 50.0
        with pytest.raises(
            RuntimeError,
            match=r"verdict_floors\.malicious.*violates its band contract",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_missing_baselines_drift_leaf_raises_with_path(
        self, tmp_path, monkeypatch
    ):
        # Leaf validation: a missing nested tunable must fail fast with its
        # full dotted path, not a raw KeyError at first use.
        cfg = _minimal_config()
        del cfg["baselines"]["drift"]["p99_threshold"]
        with pytest.raises(RuntimeError, match=r"baselines\.drift\.p99_threshold"):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_missing_baselines_windows_raises_with_path(
        self, tmp_path, monkeypatch
    ):
        cfg = _minimal_config()
        del cfg["baselines"]["windows"]["global_days"]
        with pytest.raises(RuntimeError, match=r"baselines\.windows\.global_days"):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_missing_p50_cap_spread_fraction_raises_with_path(
        self, tmp_path, monkeypatch
    ):
        # The p50-bound knob must flow through the validated loader, so its
        # absence is an import-time failure, never a silent default.
        cfg = _minimal_config()
        del cfg["baselines"]["per_script_p50_cap_spread_fraction"]
        with pytest.raises(
            RuntimeError,
            match=r"baselines\.per_script_p50_cap_spread_fraction",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_missing_asset_name_carrier_enabled_raises_with_path(
        self, tmp_path, monkeypatch
    ):
        cfg = _minimal_config()
        cfg["scorers"]["phishing"]["asset_name_carrier"] = {}
        with pytest.raises(
            RuntimeError,
            match=r"scorers\.phishing\.asset_name_carrier\.enabled",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)


class TestUnknownKeyRejection:
    """Unknown YAML keys fail at import with their dotted path: a misspelled
    tunable must not sit silently unread while the code keeps using an old
    value."""

    def test_unknown_top_level_key_raises(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        cfg["surprise_block"] = {"x": 1}
        with pytest.raises(RuntimeError, match=r"unknown keys.*surprise_block"):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_unknown_nested_scorer_key_raises(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        cfg["scorers"]["multiple_sat"]["lazy_validator_flor"] = 60.0
        with pytest.raises(
            RuntimeError,
            match=r"unknown keys.*scorers\.multiple_sat\.lazy_validator_flor",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_extra_weight_name_raises(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        cfg["scorers"]["token_dust"]["weights"]["bonus_axis"] = 0.1
        with pytest.raises(
            RuntimeError,
            match=r"unknown keys.*scorers\.token_dust\.weights\.bonus_axis",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_allowlist_subtrees_are_freeform(self, tmp_path, monkeypatch):
        # Network-keyed allowlists carry operational data, not schema; the
        # unknown-key walk must not reject their entries.
        cfg = _minimal_config()
        cfg["scorers"]["multiple_sat"]["allowlist_prefixes"] = {
            "mainnet": ["addr1qexample"], "preprod": [],
        }
        sc = _reload_module(monkeypatch, tmp_path, cfg)
        assert "multiple_sat" in sc._CFG["scorers"]


class TestAnchorWeightNameValidation:
    """A typo'd anchor or weight name fails at import with the dotted path
    named. Without this, the KeyError surfaces at SCORING time where the
    engine swallows per-tx scorer exceptions: silent recall loss."""

    def test_typod_bootstrap_anchor_fails_at_import(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        anchors = cfg["scorers"]["multiple_sat"]["bootstrap_anchors"]
        anchors["n_assets_out_of_scrpit"] = anchors.pop("n_assets_out_of_script")
        with pytest.raises(
            RuntimeError,
            match=r"scorers\.multiple_sat\.bootstrap_anchors\.n_assets_out_of_script",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_typod_weight_name_fails_at_import(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        weights = cfg["scorers"]["multiple_sat"]["weights"]
        weights["extractoin"] = weights.pop("extraction")
        with pytest.raises(
            RuntimeError,
            match=r"scorers\.multiple_sat\.weights\.extraction",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_anchor_missing_percentile_leaf_fails(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        cfg["scorers"]["token_dust"]["bootstrap_anchors"]["ada_amount"] = {"p50": 0}
        with pytest.raises(
            RuntimeError,
            match=r"scorers\.token_dust\.bootstrap_anchors\.ada_amount\.p99",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_typod_fixed_anchor_fails_at_import(self, tmp_path, monkeypatch):
        cfg = _minimal_config()
        anchors = cfg["scorers"]["circular"]["fixed_anchors"]
        anchors["entorpy"] = anchors.pop("entropy")
        with pytest.raises(
            RuntimeError,
            match=r"scorers\.circular\.fixed_anchors\.entropy",
        ):
            _reload_module(monkeypatch, tmp_path, cfg)

    def test_multiple_sat_names_match_scorer_specs(self, monkeypatch):
        # Cross-check: the loader's declared anchor names cannot drift from
        # the names the multiple_sat scorer actually resolves. Reload the
        # SHIPPED config first: importing the scorer reads real config
        # values at module import, and a sibling test may have left a
        # minimal fixture loaded.
        sc = _reload_shipped(monkeypatch)
        import app.analysis.scorers.multiple_sat as ms
        spec_features = {feature for feature, _allowed in ms._BASELINE_SPECS}
        assert spec_features == set(
            sc._SCORER_BOOTSTRAP_ANCHOR_NAMES["multiple_sat"]
        )


class TestGet:
    def test_unknown_section_raises_clear_error(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_config())
        with pytest.raises(KeyError, match="scorers.does_not_exist"):
            sc.get("does_not_exist")


class TestAnchor:
    def test_returns_p50_p99_tuple(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_config())
        container = {"foo": {"p50": 1.0, "p99": 9.0}}
        assert sc.anchor(container, "foo") == (1.0, 9.0)

    def test_coerces_ints_to_floats(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_config())
        container = {"foo": {"p50": 1, "p99": 9}}
        p50, p99 = sc.anchor(container, "foo")
        assert isinstance(p50, float) and isinstance(p99, float)


class TestPerScriptP99Cap:
    """A learned baseline's p99 is capped at K x the bootstrap anchor p99
    (baselines.per_script_p99_cap_multiplier), bounding how far a poisoned
    per-script distribution can de-sensitise any scorer."""

    def test_resolved_p99_capped(self, monkeypatch):
        sc = _sc()
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (0.0, 1_000_000.0, "per_script"),
        )
        bootstrap = {"feat": {"p50": 0.0, "p99": 2.0}}
        p50, p99, source = sc.resolved_or_bootstrap(
            "feat", "per_script", "addrA", "preprod", bootstrap, "feat",
        )
        assert source == "per_script"
        assert p99 == sc._P99_CAP_MULTIPLIER * 2.0
        # A canonical attack value (2 = the anchor p99) still normalises
        # above zero against the capped saturation point.
        from app.analysis.normalise import normalise
        assert normalise(2.0, p50=p50, p99=p99) > 0.0

    def test_uncapped_when_below_cap(self, monkeypatch):
        sc = _sc()
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (0.0, 5.0, "per_script"),
        )
        bootstrap = {"feat": {"p50": 0.0, "p99": 2.0}}
        _, p99, _ = sc.resolved_or_bootstrap(
            "feat", "per_script", "addrA", "preprod", bootstrap, "feat",
        )
        assert p99 == 5.0

    def test_bootstrap_path_not_capped(self, monkeypatch):
        sc = _sc()
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (0.0, 1.0, "missing"),
        )
        bootstrap = {"feat": {"p50": 1.0, "p99": 9.0}}
        p50, p99, source = sc.resolved_or_bootstrap(
            "feat", "per_script", "addrA", "preprod", bootstrap, "feat",
        )
        assert (p50, p99, source) == (1.0, 9.0, "bootstrap")


class TestPerScriptP50Cap:
    """normalise() subtracts p50 first, so a poisoned MEDIAN de-sensitises
    an axis exactly like a widened tail. The p50 bound is ANCHOR-relative,
    anchor_p50 + K * (anchor_p99 - anchor_p50): the previous cap-relative
    bound (~4.55x the anchor p99) left an in-bound poisoned pair enough
    room to zero a real drain below the suppression-escape floor."""

    def test_poisoned_p50_clamped_to_anchor_relative_bound(self, monkeypatch):
        sc = _sc()
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (1e12, 1e15, "per_script"),
        )
        anchor_p50, anchor_p99 = 0.0, 2.0
        bootstrap = {"feat": {"p50": anchor_p50, "p99": anchor_p99}}
        p50, p99, _ = sc.resolved_or_bootstrap(
            "feat", "per_script", "addrA", "preprod", bootstrap, "feat",
        )
        cap = sc._P99_CAP_MULTIPLIER * anchor_p99
        assert p99 == cap
        assert p50 == pytest.approx(
            anchor_p50 + sc._P50_CAP_SPREAD_FRACTION * (anchor_p99 - anchor_p50)
        )
        # The axis is not dead: an attack at the anchor p99 still normalises
        # positive instead of scoring 0 against the poisoned pair.
        from app.analysis.normalise import normalise
        assert normalise(anchor_p99, p50=p50, p99=p99) > 0.0

    def test_p50_below_bound_unchanged(self, monkeypatch):
        sc = _sc()
        anchor_p50, anchor_p99 = 0.0, 2.0
        bound = anchor_p50 + sc._P50_CAP_SPREAD_FRACTION * (anchor_p99 - anchor_p50)
        learned_p50 = bound * 0.6  # strictly inside the bound
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (learned_p50, 5.0, "per_script"),
        )
        bootstrap = {"feat": {"p50": anchor_p50, "p99": anchor_p99}}
        p50, p99, _ = sc.resolved_or_bootstrap(
            "feat", "per_script", "addrA", "preprod", bootstrap, "feat",
        )
        assert (p50, p99) == (learned_p50, 5.0)

    def test_p50_bound_robust_when_anchor_p50_zero(self, monkeypatch):
        # The bound degrades to K * anchor_p99 when the anchor p50 is 0 (the
        # n_assets case), so it can never collapse the clamp to zero.
        sc = _sc()
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (1e6, 1e6, "per_script"),
        )
        bootstrap = {"feat": {"p50": 0.0, "p99": 2.0}}
        p50, p99, _ = sc.resolved_or_bootstrap(
            "feat", "per_script", "addrA", "preprod", bootstrap, "feat",
        )
        assert p50 > 0.0
        assert p50 < p99

    def test_oversized_fraction_still_keeps_usable_spread(
        self, tmp_path, monkeypatch
    ):
        # Degenerate-pair protection: even a misconfigured K (here 10x the
        # anchor spread) cannot push the p50 bound to or above the p99 cap;
        # the min_spread_ratio term keeps the capped pair non-degenerate.
        cfg = _minimal_config()
        cfg["baselines"]["per_script_p50_cap_spread_fraction"] = 10.0
        sc = _reload_module(monkeypatch, tmp_path, cfg)
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (1e6, 1e9, "per_script"),
        )
        bootstrap = {"feat": {"p50": 0.0, "p99": 2.0}}
        p50, p99, _ = sc.resolved_or_bootstrap(
            "feat", "per_script", "addrA", "preprod", bootstrap, "feat",
        )
        cap = sc._P99_CAP_MULTIPLIER * 2.0
        assert p99 == cap
        assert p50 == pytest.approx(cap / (1.0 + sc._MIN_SPREAD_RATIO))
        assert p50 < p99

    def test_worst_case_poisoned_pair_clears_escape_floor(self, monkeypatch):
        # RECALL GUARANTEE pinning K to the shipped thresholds: under the
        # WORST capped-poisoned per-script baseline, a drain at the
        # n_assets bootstrap anchor p99 (the canonical 2-NFT double-sat
        # magnitude) must still normalise at or above the multiple_sat
        # suppression-escape floor. Every value here comes from the shipped
        # config, so retuning any knob re-runs this arithmetic.
        from app.analysis.normalise import normalise
        sc = _reload_shipped(monkeypatch)
        boot = sc.get("multiple_sat")["bootstrap_anchors"]
        anchor_p50, anchor_p99 = sc.anchor(boot, "n_assets_out_of_script")
        floor_min = float(
            sc.get("multiple_sat")["suppression_escape"]["extraction_floor_min"]
        )
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (1e9, 1e12, "per_script"),
        )
        p50, p99, _ = sc.resolved_or_bootstrap(
            "n_assets_out_of_script", "per_script", "addrA", "preprod",
            boot, "n_assets_out_of_script",
        )
        assert normalise(anchor_p99, p50=p50, p99=p99) >= floor_min
