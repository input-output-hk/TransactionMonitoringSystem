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
        scorers:
          multiple_sat:
            weights: {}
            bootstrap_anchors: {}
            allowlist_prefixes: []
            reason_threshold: 0.5
          large_datum:
            gate: {}
            weights: {}
            fixed_anchors: {}
            bootstrap_anchors: {}
            reason_threshold: 0.5
          token_dust:
            weights: {}
            bootstrap_anchors: {}
            reason_threshold: 0.5
          large_value:
            weights: {}
            bootstrap_anchors: {}
            reason_threshold: 0.5
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
            min_profit_lovelace: 200000
            high_band_cap: 79.0
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
          phishing:
            weights: {}
            fixed_anchors: {}
            bootstrap_anchors: {}
            similarity_suspicious_range: {}
            social_engineering: {}
            reason_thresholds: {}
            critical_threshold: 0.6
            metadata_labels: []
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
        from pathlib import Path
        # The resolved config_dir must match our tmp_path.
        assert sc._config_dir() == tmp_path / "config"


class TestValidation:
    def test_missing_scorers_key_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="top-level 'scorers' mapping"):
            _reload_module(monkeypatch, tmp_path, "other_root: {}")

    def test_missing_scorer_section_raises_with_path(self, tmp_path, monkeypatch):
        body = _minimal_valid_yaml().replace(
            "multiple_sat:\n    weights: {}\n    bootstrap_anchors: {}\n    allowlist_prefixes: []\n    reason_threshold: 0.5\n  ",
            "",
        )
        with pytest.raises(RuntimeError, match="scorers.multiple_sat"):
            _reload_module(monkeypatch, tmp_path, body)

    def test_missing_required_key_raises_with_path(self, tmp_path, monkeypatch):
        body = _minimal_valid_yaml().replace(
            "multiple_sat:\n    weights: {}\n    bootstrap_anchors: {}\n    allowlist_prefixes: []\n    reason_threshold: 0.5",
            "multiple_sat:\n    weights: {}\n    bootstrap_anchors: {}\n    reason_threshold: 0.5",
        )
        with pytest.raises(RuntimeError, match="scorers.multiple_sat.allowlist_prefixes"):
            _reload_module(monkeypatch, tmp_path, body)

    def test_non_dict_scorer_section_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="must be a mapping"):
            _reload_module(
                monkeypatch, tmp_path,
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
