"""The /api/v1/clustering reverse-proxy forwards a service API key to the sidecar.

Review finding: the sidecar shipped unauthenticated because the proxy forwarded
no credential. With CLUSTERING_SIDECAR_API_KEY set the proxy must present it as
X-API-Key so the sidecar can run REQUIRE_AUTH=1; with it empty, nothing is sent
(legacy zero-config behaviour).
"""

import pytest
from fastapi.testclient import TestClient


class _FakeResponse:
    content = b'{"ok": true}'
    status_code = 200
    headers = {"content-type": "application/json"}


class _FakeClient:
    """Captures the outbound request the proxy makes to the sidecar."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, params=None, content=None, headers=None):
        _FakeClient.captured = {"method": method, "url": url, "headers": dict(headers or {})}
        return _FakeResponse()


@pytest.fixture
def client(monkeypatch):
    from app.auth import api_key
    from app.config import settings
    from app.main import app

    # Open auth so the proxy's Security(verify_api_key) lets the request through
    # (mirrors the auth_open fixture used elsewhere); we test forwarding, not auth.
    monkeypatch.setattr(api_key, "_dev_mode", True)
    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
    _FakeClient.captured = {}
    return TestClient(app)


def test_forwards_api_key_when_configured(client, monkeypatch):
    from app.api import clustering
    from app.config import settings

    monkeypatch.setattr(settings, "CLUSTERING_SIDECAR_API_KEY", "sidecar-key")
    monkeypatch.setattr(clustering.httpx, "AsyncClient", _FakeClient)

    resp = client.get("/api/v1/clustering/contracts")
    assert resp.status_code == 200
    assert _FakeClient.captured["headers"].get("X-API-Key") == "sidecar-key"


def test_no_key_forwarded_when_unset(client, monkeypatch):
    from app.api import clustering
    from app.config import settings

    monkeypatch.setattr(settings, "CLUSTERING_SIDECAR_API_KEY", "")
    monkeypatch.setattr(clustering.httpx, "AsyncClient", _FakeClient)

    resp = client.get("/api/v1/clustering/contracts")
    assert resp.status_code == 200
    assert "X-API-Key" not in _FakeClient.captured["headers"]
