"""Regression test for the ``?network=`` query param on API endpoints.

Locks in the Literal["mainnet", "preprod", "preview"] contract so that
widening or narrowing the accepted set is a conscious decision tracked in
this test and in `backend/app/models/transaction.py`.

These tests only validate request routing; they don't assert on response
bodies (the scorers or DB layers might return empty results, which is
still a valid 200). What matters is the status code returned by FastAPI's
query-param validation layer.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    # Import here so patching TMS_CONFIG_DIR or CARDANO_NETWORK in
    # future sibling tests doesn't leak into app.main import time.
    from app.main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _dev_mode_auth(monkeypatch):
    """Run validation tests in dev-mode auth.

    Auth deliberately runs BEFORE query validation (no validation detail
    for unauthenticated callers), so exercising the 422 contract requires
    passing auth first; dev mode is how the suite does that regardless of
    the local .env's API_KEYS. The auth-before-validation ordering itself
    is locked in by test_unauthenticated_gets_403_before_validation.
    """
    from app import auth
    monkeypatch.setattr(auth, "_dev_mode", True)


# Endpoints that accept ?network=. (path, default_status_range).
# Every endpoint should accept each valid network and reject the invalid.
_ENDPOINTS = [
    "/api/analysis/results",
    "/api/analysis/stats",
    "/api/transactions/",
    "/api/lifecycle",
]


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
@pytest.mark.parametrize("network", ["mainnet", "preprod", "preview"])
def test_valid_networks_accepted(client, endpoint, network):
    """Every Literal value must round-trip through the router without a 422."""
    r = client.get(f"{endpoint}?network={network}")
    # 200 (normal), 404 (no data), or 500 (backend offline in CI) are all
    # acceptable signals that validation passed. 422 would indicate that
    # FastAPI rejected the Literal value.
    assert r.status_code != 422, (
        f"{endpoint}?network={network} unexpectedly failed Literal validation"
    )


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_invalid_network_rejected(client, endpoint):
    """An unknown network must be rejected at the validation layer."""
    r = client.get(f"{endpoint}?network=testnet")
    assert r.status_code == 422


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_omitted_network_accepted(client, endpoint):
    """Without ?network= the handler should fall back to CARDANO_NETWORK."""
    r = client.get(endpoint)
    assert r.status_code != 422


def test_unauthenticated_gets_403_before_validation(client, monkeypatch):
    """Auth runs before query validation: an unauthenticated caller learns
    nothing about parameter shapes (403, not 422)."""
    from app import auth
    monkeypatch.setattr(auth, "_dev_mode", False)
    monkeypatch.setattr(auth, "_valid_keys", ["sentinel-key"])
    r = client.get("/api/analysis/results?network=testnet")
    assert r.status_code == 403
