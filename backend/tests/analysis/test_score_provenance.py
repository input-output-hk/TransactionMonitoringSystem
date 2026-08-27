"""Score provenance: the config digest and the build identity on every row.

The value of `config_hash` is that a reviewer holding a `detection.yaml` can
recompute it and prove which tuning produced a stored score. That property is
only real if the documented recipe reproduces what the process reports, so it
is asserted here against the shipped file rather than against a fixture.
"""

import hashlib
import json

import pytest
import yaml

from app.analysis.engine import _build_scorers, _score_transaction
from app.analysis.scorer_config import _digest, config_hash
from app.config import settings

# Length of a hex-encoded SHA-256 digest (FIPS 180-4: 256 bits, 4 bits/char).
SHA256_HEX_LEN = 64


def _shipped_config_path():
    """The file the loader itself resolved, so the test cannot drift from it."""
    import app.analysis.scorer_config as sc

    return sc._config_dir() / "detection.yaml"


def _make_row(tx_hash="tx-provenance"):
    return {
        "tx_hash": tx_hash,
        "network": "preprod",
        "fee": 200_000,
        "input_count": 2,
        "output_count": 3,
        "total_output_value": 10_000_000,
        "metadata": None,
        "addresses": ["addr_test1qzabc"],
        "raw_data": "{}",
        "slot": 50000,
        "block_height": 1000,
        "timestamp": "2025-01-01T00:00:00Z",
    }


class TestConfigHash:
    def test_is_sha256_hex(self):
        h = config_hash()
        assert len(h) == SHA256_HEX_LEN
        assert h == h.lower()
        int(h, 16)  # raises if it is not hex

    def test_is_stable_across_calls(self):
        assert config_hash() == config_hash()

    def test_reproducible_from_the_shipped_file(self):
        """The recipe documented for reviewers must give the same answer.

        This is the whole point of the column: if an independent recomputation
        disagrees, the digest proves nothing.
        """
        cfg = yaml.safe_load(_shipped_config_path().read_text(encoding="utf-8"))
        canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str)
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert config_hash() == expected

    def test_changes_when_a_tunable_changes(self):
        cfg = yaml.safe_load(_shipped_config_path().read_text(encoding="utf-8"))
        before = _digest(cfg)
        # Any weight will do; the assertion is about the digest reacting, not
        # about this particular knob.
        cfg["scorers"]["large_value"]["weights"]["quantity_digits"] = 0.999
        assert _digest(cfg) != before

    def test_ignores_key_order(self):
        """Reordering a block without changing a value must not read as a retune."""
        cfg = yaml.safe_load(_shipped_config_path().read_text(encoding="utf-8"))
        reversed_top = {k: cfg[k] for k in reversed(list(cfg))}
        assert _digest(reversed_top) == _digest(cfg)


class TestEngineStampsProvenance:
    def test_config_hash_is_written(self):
        result = _score_transaction(_make_row(), _build_scorers())
        assert result["config_hash"] == config_hash()

    def test_code_version_comes_from_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "CODE_VERSION", "deadbee")
        result = _score_transaction(_make_row(), _build_scorers())
        assert result["code_version"] == "deadbee"

    def test_code_version_blank_when_unset(self, monkeypatch):
        """A working-tree run has no single commit identity, so it claims none."""
        monkeypatch.setattr(settings, "CODE_VERSION", "")
        result = _score_transaction(_make_row(), _build_scorers())
        assert result["code_version"] == ""

    @pytest.mark.parametrize("field", ["config_hash", "code_version"])
    def test_provenance_field_present(self, field):
        assert field in _score_transaction(_make_row(), _build_scorers())


class TestCodeVersionBinding:
    """The env variable name is load-bearing and fails silently when wrong.

    Settings declares no `env_prefix`, so the field binds from the bare
    `CODE_VERSION`. A prefixed `TMS_CODE_VERSION` (the shape `TMS_ENV` and
    `TMS_ALLOW_DEV_MODE` use, because those are read through `os.environ`
    directly) is simply ignored, and the only symptom would be every score row
    claiming no build. That is not a failure a reader would notice, so it is
    pinned here.
    """

    def test_binds_from_the_unprefixed_name(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setenv("CODE_VERSION", "abc1234")
        assert Settings().CODE_VERSION == "abc1234"

    def test_prefixed_name_is_not_bound(self, monkeypatch):
        from app.config import Settings

        monkeypatch.delenv("CODE_VERSION", raising=False)
        monkeypatch.setenv("TMS_CODE_VERSION", "wrong-name")
        assert Settings().CODE_VERSION == ""
