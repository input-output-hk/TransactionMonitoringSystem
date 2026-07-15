"""Regression tests for the /health + /health/detail split.

Background: a prior revision exposed operational internals (network name,
Ogmios sync lag, WebSocket connection count, pipeline circuit-breaker state)
on an unauthenticated /health endpoint. The split moves that detail behind
API-key auth while keeping /health as a minimal liveness probe.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


class TestHealthMinimal:
    def test_health_returns_only_status(self, client):
        """Unauthenticated /health must not leak any operational internal."""
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body == {"status": "healthy"}
        # Explicitly assert none of the previously-leaked fields reappear.
        for forbidden in ("network", "connections", "pipeline_state", "ogmios"):
            assert forbidden not in body


class TestHealthDetail:
    def test_health_detail_requires_auth_when_keys_configured(
        self,
        client,
        monkeypatch,
    ):
        """With API_KEYS set, /health/detail must reject unauthenticated calls."""
        from app.auth import api_key

        monkeypatch.setattr(api_key, "_valid_keys", ["sentinel-key"])
        monkeypatch.setattr(api_key, "_dev_mode", False)
        r = client.get("/health/detail")
        # 401: unauthenticated, so the SPA's session-expiry redirect fires.
        assert r.status_code == 401

    def test_health_detail_open_in_dev_mode(self, client, monkeypatch):
        """With no API keys (dev mode), /health/detail is open — same policy
        as the rest of the API under the existing _dev_mode behaviour."""
        from app.auth import api_key

        monkeypatch.setattr(api_key, "_valid_keys", [])
        monkeypatch.setattr(api_key, "_dev_mode", True)
        r = client.get("/health/detail")
        assert r.status_code == 200
        body = r.json()
        assert "network" in body
        assert "pipeline_state" in body
