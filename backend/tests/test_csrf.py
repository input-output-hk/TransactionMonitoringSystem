"""CSRF double-submit cookie check (app.csrf.CSRFMiddleware): defense-in-depth
on top of SameSite=Lax. A mutating request that carries the session cookie
must also echo the CSRF cookie's value in a header.

Uses POST /api/auth/logout as the drive-through endpoint — it is a simple
mutating route gated only on the session cookie being present (not on a
valid session), so the DB call it makes is patched out and the test isolates
the middleware itself.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _set_cookies(client, **cookies):
    for name, value in cookies.items():
        client.cookies.set(name, value)


def _post_logout(client, headers=None):
    with patch("app.api.auth.delete_session", AsyncMock()):
        return client.post("/api/auth/logout", headers=headers or {})


class TestCSRFGate:
    def test_matching_cookie_and_header_passes(self, client):
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", settings.CSRF_COOKIE_NAME: "tok123"},
        )
        resp = _post_logout(client, headers={settings.CSRF_HEADER_NAME: "tok123"})
        assert resp.status_code == 200

    def test_missing_header_is_rejected(self, client):
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", settings.CSRF_COOKIE_NAME: "tok123"},
        )
        resp = _post_logout(client)
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["detail"]

    def test_mismatched_header_is_rejected(self, client):
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", settings.CSRF_COOKIE_NAME: "tok123"},
        )
        resp = _post_logout(client, headers={settings.CSRF_HEADER_NAME: "wrong"})
        assert resp.status_code == 403

    def test_missing_csrf_cookie_is_rejected(self, client):
        _set_cookies(client, **{settings.SESSION_COOKIE_NAME: "sess"})
        resp = _post_logout(client, headers={settings.CSRF_HEADER_NAME: "tok123"})
        assert resp.status_code == 403

    def test_no_session_cookie_is_out_of_scope(self, client):
        """No session cookie = not cookie-authed traffic (e.g. an API-key
        caller); the CSRF check does not apply regardless of header state."""
        resp = _post_logout(client)
        assert resp.status_code == 200

    def test_get_requests_are_never_blocked(self, client):
        # /health needs no auth/DB, so a 403 here could only come from the
        # CSRF middleware itself misfiring on a safe method.
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", settings.CSRF_COOKIE_NAME: "tok123"},
        )
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_disabled_flag_bypasses_the_check_entirely(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CSRF_PROTECTION_ENABLED", False)
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", settings.CSRF_COOKIE_NAME: "tok123"},
        )
        resp = _post_logout(client)
        assert resp.status_code == 200
