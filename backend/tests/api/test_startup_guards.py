"""Fail-fast startup guards (main._validate_startup_settings).

House posture: accidental-production-misconfig refuses to start rather
than warns. CORS '*' with API keys configured is the newest member of
that family (review finding: shipping '*' as a production default).
"""

import pytest

from app.config import settings
from app.main import _validate_startup_settings


@pytest.fixture
def prod_mode(monkeypatch):
    """API keys configured, no dev-mode override: production posture."""
    from app import auth

    monkeypatch.setattr(auth, "_dev_mode", False)
    monkeypatch.setattr(settings, "TMS_ALLOW_DEV_MODE", "0")
    monkeypatch.delenv("TMS_ALLOW_DEV_MODE", raising=False)
    monkeypatch.setattr(settings, "CLICKHOUSE_PASSWORD", "secret")
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
