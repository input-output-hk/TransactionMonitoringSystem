"""Tests for the detection config loader."""

import importlib
import textwrap

import pytest


def _reload_module(monkeypatch, tmp_path, yaml_body: str):
    """Write yaml_body to a temp detection.yaml and reload scorer_config."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "detection.yaml").write_text(yaml_body, encoding="utf-8")
    monkeypatch.setenv("TMS_CONFIG_DIR", str(cfg_dir))
    import app.analysis.scorer_config as sc
    return importlib.reload(sc)


def _minimal_valid_yaml() -> str:
    """YAML that satisfies every scorer's required keys (empty-but-present values)."""
    return textwrap.dedent("""\
        protocol_limits:
          max_value_size_bytes: 5000
          max_tx_size_bytes: 16384
        composite_corroboration:
          corroboration_threshold: 40.0
        baselines:
          min_spread_ratio: 0.10
          per_script_p99_cap_multiplier: 5.0
          drift:
            enabled: true
            p99_threshold: 0.50
        scorers:
          multiple_sat:
            weights: {}
            bootstrap_anchors: {}
            allowlist_prefixes: []
            reason_threshold: 0.5
            lazy_validator_threshold: 0.8
            lazy_validator_floor: 60.0
            lazy_validator_extraction_min: 0.05
            per_script_extraction_headroom: 3.0
            uniform_sweep_guard:
              enabled: true
              require_uniform_redeemer: true
              require_no_script_return: true
              min_inputs: 10
            suppression_escape:
              enabled: true
              extraction_floor_min: 0.5
          large_datum:
            gate:
              flag_datum_hash_only: true
            weights: {}
            fixed_anchors: {}
            bootstrap_anchors: {}
            aggregate_engagement_min: 12000
            reason_threshold: 0.5
          token_dust:
            gate:
              min_token_count: 2
            weights: {}
            bootstrap_anchors: {}
            allowlist_prefixes: {}
            allowlist_policies: {}
            dos_asset_min: 15
            reason_threshold: 0.5
          large_value:
            weights: {}
            bootstrap_anchors: {}
            reason_threshold: 0.5
            min_digits_subscore: 0.05
          front_running:
            weights: {}
            fixed_anchors: {}
            bootstrap_anchors: {}
            outcome_scores: {}
            reason_thresholds: {}
            min_recurrence_wins: 3
            high_band_cap: 79.0
            delta_ms_default: 10000
          sandwich:
            weights: {}
            fixed_anchors: {}
            bootstrap_anchors: {}
            link_scores: {}
            window_slots: 5
            neighbor_limit: 20
            min_profit_lovelace: 200000
            reason_thresholds: {}
          circular:
            weights: {}
            fixed_anchors: {}
            bootstrap_anchors: {}
            cycle: {}
            reason_threshold: 0.5
            moderate_cap: 59.0
          fake_token:
            weights: {}
            fixed_anchors: {}
            bootstrap_anchors: {}
            similarity_threshold: 0.8
            unicode_scores: {}
            reason_thresholds: {}
            critical_assets:
              multiplier: 1.8
              names: []
            ascii_homoglyphs_enabled: true
          phishing:
            weights: {}
            fixed_anchors: {}
            bootstrap_anchors: {}
            similarity_suspicious_range: {}
            social_engineering: {}
            reason_thresholds: {}
            critical_threshold: 0.6
            metadata_labels: []
            asset_name_carrier:
              enabled: true
            min_decoded_string_len: 4
        """)


class TestLoader:
    def test_loads_detection_yaml(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_valid_yaml())
        assert "multiple_sat" in sc._CFG["scorers"]

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        monkeypatch.setenv("TMS_CONFIG_DIR", str(cfg_dir))
        import app.analysis.scorer_config as sc
        with pytest.raises(RuntimeError, match="Detection config not found"):
            importlib.reload(sc)

    def test_env_override_honoured(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_valid_yaml())
        # The resolved config_dir must match our tmp_path.
        assert sc._config_dir() == tmp_path / "config"


class TestValidation:
    def test_missing_scorers_key_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="top-level 'scorers' mapping"):
            _reload_module(monkeypatch, tmp_path, "other_root: {}")

    def test_missing_scorer_section_raises_with_path(self, tmp_path, monkeypatch):
        # Delete the entire multiple_sat block. Built by line-filter rather
        # than a fragile string replace so additions to the minimal YAML
        # (extra required keys etc.) do not silently break this test.
        lines = _minimal_valid_yaml().splitlines(keepends=True)
        out: list[str] = []
        in_block = False
        for line in lines:
            if line.startswith("  multiple_sat:"):
                in_block = True
                continue
            if in_block:
                # A new sibling scorer block starts at the same 2-space indent.
                if line.startswith("  ") and not line.startswith("    ") and line.strip():
                    in_block = False
                    out.append(line)
                continue
            out.append(line)
        body = "".join(out)
        with pytest.raises(RuntimeError, match="scorers.multiple_sat"):
            _reload_module(monkeypatch, tmp_path, body)

    def test_missing_required_key_raises_with_path(self, tmp_path, monkeypatch):
        body = _minimal_valid_yaml().replace(
            "multiple_sat:\n    weights: {}\n    bootstrap_anchors: {}\n    allowlist_prefixes: []\n    reason_threshold: 0.5",
            "multiple_sat:\n    weights: {}\n    bootstrap_anchors: {}\n    reason_threshold: 0.5",
        )
        with pytest.raises(RuntimeError, match="scorers.multiple_sat.allowlist_prefixes"):
            _reload_module(monkeypatch, tmp_path, body)

    def test_missing_nested_required_key_raises_with_full_path(self, tmp_path, monkeypatch):
        # The token_dust scorer requires gate.min_token_count. Removing only
        # the leaf field (while leaving the gate block in place) must surface
        # the dotted path, not a downstream KeyError at scorer import time.
        body = _minimal_valid_yaml().replace(
            "  token_dust:\n    gate:\n      min_token_count: 2",
            "  token_dust:\n    gate: {}",
        )
        with pytest.raises(RuntimeError, match="scorers.token_dust.gate.min_token_count"):
            _reload_module(monkeypatch, tmp_path, body)

    def test_missing_protocol_limits_raises(self, tmp_path, monkeypatch):
        # Strip the protocol_limits block from the otherwise-valid minimal YAML.
        body = "\n".join(
            line for line in _minimal_valid_yaml().splitlines()
            if not line.startswith("protocol_limits")
            and "max_value_size_bytes" not in line
            and "max_tx_size_bytes" not in line
        )
        with pytest.raises(RuntimeError, match="top-level 'protocol_limits' mapping"):
            _reload_module(monkeypatch, tmp_path, body)

    def test_non_dict_scorer_section_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="must be a mapping"):
            _reload_module(
                monkeypatch, tmp_path,
                "protocol_limits:\n  max_value_size_bytes: 5000\n"
                "  max_tx_size_bytes: 16384\n"
                "composite_corroboration:\n  corroboration_threshold: 40.0\n"
                "baselines:\n  min_spread_ratio: 0.10\n"
                "  per_script_p99_cap_multiplier: 5.0\n"
                "  drift:\n    enabled: true\n    p99_threshold: 0.50\n"
                "scorers:\n  multiple_sat: 'not a mapping'\n",
            )


class TestGet:
    def test_unknown_section_raises_clear_error(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_valid_yaml())
        with pytest.raises(KeyError, match="scorers.does_not_exist"):
            sc.get("does_not_exist")


class TestAnchor:
    def test_returns_p50_p99_tuple(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_valid_yaml())
        container = {"foo": {"p50": 1.0, "p99": 9.0}}
        assert sc.anchor(container, "foo") == (1.0, 9.0)

    def test_coerces_ints_to_floats(self, tmp_path, monkeypatch):
        sc = _reload_module(monkeypatch, tmp_path, _minimal_valid_yaml())
        container = {"foo": {"p50": 1, "p99": 9}}
        p50, p99 = sc.anchor(container, "foo")
        assert isinstance(p50, float) and isinstance(p99, float)


class TestPerScriptP99Cap:
    """A learned baseline's p99 is capped at K x the bootstrap anchor p99
    (baselines.per_script_p99_cap_multiplier), bounding how far a poisoned
    per-script distribution can de-sensitise any scorer."""

    def test_resolved_p99_capped(self, monkeypatch):
        import app.analysis.scorer_config as sc
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
        import app.analysis.scorer_config as sc
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
        import app.analysis.scorer_config as sc
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
    an axis exactly like a widened tail; the p99 cap alone left that vector
    open (review finding). The p50 bound derives from the capped p99 and
    min_spread_ratio, never from the anchor p50 (legitimately 0 for
    count-like features)."""

    def test_poisoned_p50_clamped_to_derived_bound(self, monkeypatch):
        import app.analysis.scorer_config as sc
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (1e12, 1e15, "per_script"),
        )
        bootstrap = {"feat": {"p50": 0.0, "p99": 2.0}}
        p50, p99, _ = sc.resolved_or_bootstrap(
            "feat", "per_script", "addrA", "preprod", bootstrap, "feat",
        )
        cap = sc._P99_CAP_MULTIPLIER * 2.0
        assert p99 == cap
        assert p50 == pytest.approx(cap / (1.0 + sc._MIN_SPREAD_RATIO))
        # The axis is not dead: an attack above the capped p99 still
        # normalises positive instead of scoring 0 against the poisoned pair.
        from app.analysis.normalise import normalise
        assert normalise(12.0, p50=p50, p99=p99) > 0.0

    def test_p50_below_bound_unchanged(self, monkeypatch):
        import app.analysis.scorer_config as sc
        monkeypatch.setattr(
            sc, "resolve_baseline",
            lambda *a, **k: (2.0, 5.0, "per_script"),
        )
        bootstrap = {"feat": {"p50": 0.0, "p99": 2.0}}
        p50, p99, _ = sc.resolved_or_bootstrap(
            "feat", "per_script", "addrA", "preprod", bootstrap, "feat",
        )
        assert (p50, p99) == (2.0, 5.0)

    def test_p50_bound_robust_when_anchor_p50_zero(self, monkeypatch):
        # The bound comes from the capped p99, so an anchor p50 of 0 (the
        # n_assets case) cannot collapse the clamp to zero.
        import app.analysis.scorer_config as sc
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
