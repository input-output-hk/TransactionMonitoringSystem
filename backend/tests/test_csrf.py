"""CSRF double-submit cookie check (app.csrf.CSRFMiddleware): defense-in-depth
on top of SameSite=Lax. A mutating request that carries the session cookie
must also echo the CSRF cookie's value in a header.

The middleware runs BEFORE routing, so a nonexistent POST path isolates it
hermetically: a request the middleware rejects gets 403, one it passes gets
404 from the router — no auth or DB in the way.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.main import app

# Deliberately unrouted: 404 = the middleware let the request through.
_PROBE_PATH = "/api/v1/csrf-middleware-probe"
_PASSED_THROUGH = 404


@pytest.fixture
def client():
    return TestClient(app)


def _set_cookies(client, **cookies):
    for name, value in cookies.items():
        client.cookies.set(name, value)


class TestCSRFGate:
    def test_matching_cookie_and_header_passes(self, client):
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", CSRF_COOKIE_NAME: "tok123"},
        )
        resp = client.post(_PROBE_PATH, headers={CSRF_HEADER_NAME: "tok123"})
        assert resp.status_code == _PASSED_THROUGH

    def test_missing_header_is_rejected(self, client):
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", CSRF_COOKIE_NAME: "tok123"},
        )
        resp = client.post(_PROBE_PATH)
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["detail"]

    def test_mismatched_header_is_rejected(self, client):
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", CSRF_COOKIE_NAME: "tok123"},
        )
        resp = client.post(_PROBE_PATH, headers={CSRF_HEADER_NAME: "wrong"})
        assert resp.status_code == 403

    def test_missing_csrf_cookie_is_rejected(self, client):
        _set_cookies(client, **{settings.SESSION_COOKIE_NAME: "sess"})
        resp = client.post(_PROBE_PATH, headers={CSRF_HEADER_NAME: "tok123"})
        assert resp.status_code == 403

    def test_no_session_cookie_is_out_of_scope(self, client):
        """No session cookie = not cookie-authed traffic (e.g. an API-key
        caller); the CSRF check does not apply regardless of header state."""
        resp = client.post(_PROBE_PATH)
        assert resp.status_code == _PASSED_THROUGH

    def test_get_requests_are_never_blocked(self, client):
        # /health needs no auth/DB, so a 403 here could only come from the
        # CSRF middleware itself misfiring on a safe method.
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", CSRF_COOKIE_NAME: "tok123"},
        )
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_disabled_flag_bypasses_the_check_entirely(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CSRF_PROTECTION_ENABLED", False)
        _set_cookies(
            client,
            **{settings.SESSION_COOKIE_NAME: "sess", CSRF_COOKIE_NAME: "tok123"},
        )
        resp = client.post(_PROBE_PATH)
        assert resp.status_code == _PASSED_THROUGH


class TestLogoutExemption:
    def test_logout_passes_without_any_csrf_material(self, client):
        """A session issued before the CSRF cookie existed must always be
        able to escape via logout and re-login — otherwise a deploy of the
        CSRF check leaves every pre-existing session stuck authenticated
        with all mutating requests rejected (review finding)."""
        _set_cookies(client, **{settings.SESSION_COOKIE_NAME: "legacy-sess"})
        with patch("app.api.auth.delete_session", AsyncMock()):
            resp = client.post("/api/v1/auth/logout")
        assert resp.status_code == 200
