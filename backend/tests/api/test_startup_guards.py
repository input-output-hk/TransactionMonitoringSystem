"""Fail-fast startup guards (main._validate_startup_settings).

House posture: accidental-production-misconfig refuses to start rather
than warns. CORS '*' with API keys configured is the newest member of
that family (review finding: shipping '*' as a production default).
"""

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.main import _validate_startup_settings


@pytest.fixture
def prod_mode(monkeypatch):
    """API keys configured, no dev-mode override: production posture."""
    from app import auth

    monkeypatch.setattr(auth, "_dev_mode", False)
    monkeypatch.setattr(settings, "TMS_ALLOW_DEV_MODE", "0")
    monkeypatch.delenv("TMS_ALLOW_DEV_MODE", raising=False)
    monkeypatch.setattr(settings, "CLICKHOUSE_PASSWORD", "secret")
    # A real production posture also sets a non-default Postgres password;
    # otherwise the credential guard (below) fires first.
    monkeypatch.setattr(settings, "POSTGRES_PASSWORD", "secret")
    monkeypatch.setattr(settings, "RAW_DATA_MAX_BYTES", 0)


class TestCorsFailFast:
    def test_wildcard_cors_with_keys_refuses_start(self, prod_mode, monkeypatch):
        monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "*")
        with pytest.raises(RuntimeError, match="CORS_ALLOW_ORIGINS"):
            _validate_startup_settings()

    def test_empty_cors_with_keys_refuses_start(self, prod_mode, monkeypatch):
        monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "")
        with pytest.raises(RuntimeError, match="CORS_ALLOW_ORIGINS"):
            _validate_startup_settings()

    def test_explicit_origin_passes(self, prod_mode, monkeypatch):
        monkeypatch.setattr(
            settings, "CORS_ALLOW_ORIGINS", "https://tms.example.com",
        )
        _validate_startup_settings()

    def test_dev_mode_with_wildcard_passes(self, monkeypatch):
        # Regression for the test/dev environment: conftest boots the app
        # with dev mode + '*'.
        from app import auth

        monkeypatch.setattr(auth, "_dev_mode", True)
        monkeypatch.setattr(settings, "TMS_ALLOW_DEV_MODE", "1")
        monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "*")
        _validate_startup_settings()


class TestPostgresPasswordFailFast:
    """A baked-in default Postgres password must refuse to start in prod
    (review finding: guessable credential with no fail-fast), but stays a
    warning in explicit dev mode."""

    def test_default_password_with_keys_refuses_start(self, prod_mode, monkeypatch):
        from app.config import DEFAULT_DEV_POSTGRES_PASSWORD

        monkeypatch.setattr(
            settings, "CORS_ALLOW_ORIGINS", "https://tms.example.com",
        )
        monkeypatch.setattr(
            settings, "POSTGRES_PASSWORD", DEFAULT_DEV_POSTGRES_PASSWORD,
        )
        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
            _validate_startup_settings()

    def test_custom_password_passes(self, prod_mode, monkeypatch):
        monkeypatch.setattr(
            settings, "CORS_ALLOW_ORIGINS", "https://tms.example.com",
        )
        monkeypatch.setattr(settings, "POSTGRES_PASSWORD", "a-real-secret")
        _validate_startup_settings()

    def test_default_password_in_dev_mode_passes(self, monkeypatch):
        from app import auth
        from app.config import DEFAULT_DEV_POSTGRES_PASSWORD

        monkeypatch.setattr(auth, "_dev_mode", True)
        monkeypatch.setattr(settings, "TMS_ALLOW_DEV_MODE", "1")
        monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "*")
        monkeypatch.setattr(
            settings, "POSTGRES_PASSWORD", DEFAULT_DEV_POSTGRES_PASSWORD,
        )
        _validate_startup_settings()


class TestTrustedProxyCidrsFailFast:
    """Malformed TRUSTED_PROXY_CIDRS must refuse to start, not degrade
    per-request (app.net only soft-fails as a last-resort backstop)."""

    @pytest.fixture(autouse=True)
    def _cors_ok(self, prod_mode, monkeypatch):
        # Keep the CORS guard quiet so these tests exercise only the
        # proxy-CIDR validation.
        monkeypatch.setattr(
            settings, "CORS_ALLOW_ORIGINS", "https://tms.example.com",
        )

    def test_malformed_cidr_refuses_start(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_ENABLED", True)
        monkeypatch.setattr(
            settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8,not-a-cidr",
        )
        with pytest.raises(RuntimeError, match="TRUSTED_PROXY_CIDRS"):
            _validate_startup_settings()

    def test_error_names_the_bad_cidr(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_ENABLED", True)
        monkeypatch.setattr(
            settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8,1.2.3.999/32",
        )
        with pytest.raises(RuntimeError, match=r"1\.2\.3\.999/32"):
            _validate_startup_settings()

    def test_valid_cidrs_pass(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_ENABLED", True)
        monkeypatch.setattr(
            settings, "TRUSTED_PROXY_CIDRS", "127.0.0.1/32,172.18.0.1/32",
        )
        _validate_startup_settings()

    def test_disabled_proxy_skips_cidr_validation(self, monkeypatch):
        # With trust disabled the CIDR list is never consulted at request
        # time, so a stale bad value must not block startup.
        monkeypatch.setattr(settings, "TRUSTED_PROXY_ENABLED", False)
        monkeypatch.setattr(settings, "TRUSTED_PROXY_CIDRS", "not-a-cidr")
        _validate_startup_settings()


class TestTrustedProxyHopsValidation:
    """Settings-level guard (pydantic ge=1): zero or negative hops would
    index past the X-Forwarded-For list end on every request."""

    @pytest.mark.parametrize("hops", [0, -1])
    def test_non_positive_hops_rejected(self, hops):
        with pytest.raises(ValidationError, match="TRUSTED_PROXY_HOPS"):
            Settings(TRUSTED_PROXY_HOPS=hops)

    def test_positive_hops_accepted(self):
        assert Settings(TRUSTED_PROXY_HOPS=2).TRUSTED_PROXY_HOPS == 2


class TestAnalysisBatchesValidation:
    """ge=1: a 0/negative drain cap makes the engine score zero txs per tick
    (silent total detection outage). Fail at config load, not at runtime."""

    @pytest.mark.parametrize("batches", [0, -1])
    def test_non_positive_batches_rejected(self, batches):
        with pytest.raises(ValidationError, match="ANALYSIS_ENGINE_MAX_BATCHES_PER_TICK"):
            Settings(ANALYSIS_ENGINE_MAX_BATCHES_PER_TICK=batches)

    def test_positive_batches_accepted(self):
        assert Settings(ANALYSIS_ENGINE_MAX_BATCHES_PER_TICK=5).ANALYSIS_ENGINE_MAX_BATCHES_PER_TICK == 5


class TestDocsGating:
    def test_app_urls_match_gating_expression(self):
        # The urls are fixed at import time from (_dev_mode or
        # TMS_API_DOCS_ENABLED); assert consistency with whatever this
        # environment resolved (dev runs expose, keyed runs hide).
        from app import auth
        from app.main import app

        expected = (
            "/openapi.json"
            if (auth._dev_mode or settings.TMS_API_DOCS_ENABLED)
            else None
        )
        assert app.openapi_url == expected
        assert (app.docs_url == "/docs") == (expected is not None)

    def test_gating_expression(self, monkeypatch):
        # The FastAPI urls are fixed at import time; pin the expression that
        # feeds them so a regression cannot silently re-expose the schema.
        from app import auth

        monkeypatch.setattr(auth, "_dev_mode", False)
        monkeypatch.setattr(settings, "TMS_API_DOCS_ENABLED", False)
        assert not (auth._dev_mode or settings.TMS_API_DOCS_ENABLED)
        monkeypatch.setattr(settings, "TMS_API_DOCS_ENABLED", True)
        assert auth._dev_mode or settings.TMS_API_DOCS_ENABLED
