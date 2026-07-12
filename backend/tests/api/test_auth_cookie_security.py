"""Session-cookie security: the Secure flag must not be spoofable by anyone
who can reach the app directly (review finding, same class as the client-IP
spoofing issue app.net guards against) — X-Forwarded-Proto is honoured only
when the direct TCP peer is a configured trusted proxy — and the CSRF
double-submit companion cookie must be issued/cleared alongside the session
cookie (see app.csrf).
"""

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.api.auth import _clear_session_cookie, _is_secure_request, _set_session_cookie
from app.config import settings


def _request(scheme="http", headers=None, client=("203.0.113.9", 12345)):
    raw_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or [])
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "scheme": scheme,
        "headers": raw_headers,
        "client": client,
        "server": ("testserver", 443 if scheme == "https" else 80),
        "query_string": b"",
        "http_version": "1.1",
        "root_path": "",
    }
    return Request(scope)


@pytest.fixture
def proxy_enabled(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_ENABLED", True)
    monkeypatch.setattr(
        settings, "TRUSTED_PROXY_CIDRS", "127.0.0.1/32,10.0.0.0/8",
    )


class TestDirectScheme:
    def test_direct_https_is_secure(self):
        assert _is_secure_request(_request(scheme="https")) is True

    def test_direct_http_is_insecure(self):
        assert _is_secure_request(_request(scheme="http")) is False


class TestForwardedProtoTrustGate:
    def test_forwarded_https_honoured_from_trusted_proxy(self, proxy_enabled):
        req = _request(
            scheme="http",
            headers=[("X-Forwarded-Proto", "https")],
            client=("10.0.0.5", 443),
        )
        assert _is_secure_request(req) is True

    def test_forwarded_https_ignored_from_untrusted_direct_peer(self):
        """The original bug: an attacker who can reach the app directly
        (bypassing the intended reverse proxy) could force Secure=True by
        forging X-Forwarded-Proto on a genuinely plaintext connection."""
        req = _request(
            scheme="http",
            headers=[("X-Forwarded-Proto", "https")],
            client=("203.0.113.9", 443),
        )
        assert _is_secure_request(req) is False

    def test_forwarded_https_ignored_when_proxy_trust_disabled(self):
        req = _request(
            scheme="http",
            headers=[("X-Forwarded-Proto", "https")],
            client=("10.0.0.5", 443),  # would be trusted if the flag were on
        )
        assert _is_secure_request(req) is False

    def test_forwarded_http_from_trusted_proxy_stays_insecure(self, proxy_enabled):
        req = _request(
            scheme="http",
            headers=[("X-Forwarded-Proto", "http")],
            client=("10.0.0.5", 443),
        )
        assert _is_secure_request(req) is False


class TestCSRFCookieIssuance:
    """_set_session_cookie / _clear_session_cookie must (un)set the CSRF
    double-submit companion alongside the session cookie — app.csrf relies
    on both existing together."""

    def test_set_session_cookie_also_sets_csrf_cookie(self):
        response = Response()
        _set_session_cookie(_request(scheme="https"), response, "sess-id")
        cookies = "\n".join(response.headers.getlist("set-cookie"))
        assert f"{settings.SESSION_COOKIE_NAME}=sess-id" in cookies
        assert f"{settings.CSRF_COOKIE_NAME}=" in cookies
        # The CSRF cookie must NOT be HttpOnly — the SPA needs to read it.
        csrf_line = next(
            line for line in response.headers.getlist("set-cookie")
            if line.startswith(f"{settings.CSRF_COOKIE_NAME}=")
        )
        assert "HttpOnly" not in csrf_line

    def test_set_session_cookie_skips_csrf_cookie_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "CSRF_PROTECTION_ENABLED", False)
        response = Response()
        _set_session_cookie(_request(scheme="https"), response, "sess-id")
        cookies = "\n".join(response.headers.getlist("set-cookie"))
        assert f"{settings.CSRF_COOKIE_NAME}=" not in cookies

    def test_clear_session_cookie_also_clears_csrf_cookie(self):
        response = Response()
        _clear_session_cookie(_request(scheme="https"), response)
        cookies = "\n".join(response.headers.getlist("set-cookie"))
        assert settings.SESSION_COOKIE_NAME in cookies
        assert settings.CSRF_COOKIE_NAME in cookies
