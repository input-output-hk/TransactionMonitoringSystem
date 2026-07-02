"""Admin notification-config API: RBAC, validation, and cache refresh.

DB + audit + cache-refresh are mocked so no live Postgres is needed; the
TestClient is used without lifespan (matching the other api tests).
"""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.auth.deps import current_user, require_admin
from app.main import app

VALID = {
    "version": 1,
    "channels": {"email": {"enabled": True, "recipients": ["a@x.com"]}},
    "triggers": {"defaults": {"Critical": ["email"]}, "rules": []},
}


@pytest.fixture
def mocked(monkeypatch):
    import app.api.notifications_config as api

    get_mock = AsyncMock(return_value=None)
    set_mock = AsyncMock()
    refresh_mock = AsyncMock()
    audit_mock = AsyncMock()
    monkeypatch.setattr(api.postgres, "get_notification_config", get_mock)
    monkeypatch.setattr(api.postgres, "set_notification_config", set_mock)
    monkeypatch.setattr(api.notif_config, "refresh_from_db", refresh_mock)
    monkeypatch.setattr(api.audit, "record", audit_mock)
    yield {"set": set_mock, "refresh": refresh_mock, "audit": audit_mock}
    app.dependency_overrides.pop(require_admin, None)


def _as_admin():
    app.dependency_overrides[require_admin] = lambda: {
        "email": "admin@x.com", "role": "Admin", "id": "00000000-0000-0000-0000-000000000001",
    }


def test_put_requires_auth(mocked):
    # No admin override + no session cookie → require_admin chain → 401.
    client = TestClient(app)
    r = client.put("/api/notifications/config", json=VALID)
    assert r.status_code == 401
    mocked["set"].assert_not_awaited()


def test_put_forbidden_for_reviewer(mocked):
    # An authenticated non-admin (Reviewer) must be rejected by require_admin
    # with 403 — and nothing is persisted/refreshed. Override the UPSTREAM
    # current_user so the real require_admin actually runs (rather than the
    # tests' usual bypass of require_admin itself).
    app.dependency_overrides[current_user] = lambda: {
        "email": "rev@x.com", "role": "Reviewer",
        "id": "00000000-0000-0000-0000-000000000002",
    }
    try:
        client = TestClient(app)
        r = client.put("/api/notifications/config", json=VALID)
        assert r.status_code == 403
        mocked["set"].assert_not_awaited()
        mocked["refresh"].assert_not_awaited()
    finally:
        app.dependency_overrides.pop(current_user, None)


def test_put_valid_persists_and_refreshes(mocked):
    _as_admin()
    client = TestClient(app)
    r = client.put("/api/notifications/config", json=VALID)
    assert r.status_code == 200
    mocked["set"].assert_awaited_once()
    mocked["refresh"].assert_awaited_once()
    mocked["audit"].assert_awaited_once()


def test_put_invalid_is_422_and_does_not_persist(mocked):
    _as_admin()
    client = TestClient(app)
    bad = {"version": 1, "channels": {}, "triggers": {"defaults": {}}}  # empty channels
    r = client.put("/api/notifications/config", json=bad)
    assert r.status_code == 422
    mocked["set"].assert_not_awaited()
    mocked["refresh"].assert_not_awaited()


def test_get_returns_config_and_secret_status(mocked):
    _as_admin()
    client = TestClient(app)
    r = client.get("/api/notifications/config")
    assert r.status_code == 200
    body = r.json()
    assert "config" in body
    assert set(body["secrets_status"]) == {
        "webhook_signing_secret_configured", "smtp_configured",
    }
    # The UI gates the read-time-only contract_anomaly attack class on this flag.
    assert isinstance(body["clustering_enabled"], bool)
