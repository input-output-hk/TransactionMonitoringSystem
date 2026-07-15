"""Shared fixtures for the API test package.

pytest makes every fixture here available to all tests under
``backend/tests/api/`` without an explicit import, so the dev-mode auth
fixture lives in one place instead of being copy-defined per module.
"""

import pytest


@pytest.fixture
def auth_open(monkeypatch):
    """Run the API in dev mode (no API key required) for happy-path tests.

    Empty ``_valid_keys`` + ``_dev_mode=True`` is what ``verify_api_key``
    treats as open access; production refuses to boot in that state (see
    ``_validate_startup_settings``), so it is strictly a test affordance.
    """
    from app.auth import api_key

    monkeypatch.setattr(api_key, "_valid_keys", [])
    monkeypatch.setattr(api_key, "_dev_mode", True)
