"""The session cookie's Secure flag must not be spoofable by anyone who can
reach the app directly (review finding, same class as the client-IP
spoofing issue app.net guards against): X-Forwarded-Proto is honoured only
when the direct TCP peer is a configured trusted proxy.
"""

import pytest
from starlette.requests import Request

from app.api.auth import _is_secure_request
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
