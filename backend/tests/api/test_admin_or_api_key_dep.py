"""require_admin_or_api_key: gates the clustering proxy's mutations. Allows an
API key or dev-mode or an Admin session; REJECTS a non-admin Reviewer session
(the gap that let a Reviewer delete contracts / enqueue jobs via the proxy)."""

import pytest
from fastapi import HTTPException

from app.auth import api_key as ak
from app.auth import deps
from app.config import settings

pytestmark = pytest.mark.asyncio


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


async def test_dev_mode_allows(monkeypatch):
    monkeypatch.setattr(ak, "_dev_mode", True)
    assert await deps.require_admin_or_api_key(_Req()) == "dev-mode"


async def test_valid_api_key_allows(monkeypatch):
    monkeypatch.setattr(ak, "_dev_mode", False)
    monkeypatch.setattr(ak, "is_valid_api_key", lambda k: k == "good")
    r = _Req({settings.API_KEY_HEADER: "good"})
    assert await deps.require_admin_or_api_key(r) == "good"


async def test_reviewer_session_rejected(monkeypatch):
    monkeypatch.setattr(ak, "_dev_mode", False)
    monkeypatch.setattr(ak, "is_valid_api_key", lambda k: False)

    async def fake_cu(_request):
        return {"id": 7, "role": "Reviewer"}

    monkeypatch.setattr(deps, "current_user", fake_cu)
    with pytest.raises(HTTPException) as ei:
        await deps.require_admin_or_api_key(_Req())
    assert ei.value.status_code == 403


async def test_admin_session_allows(monkeypatch):
    monkeypatch.setattr(ak, "_dev_mode", False)
    monkeypatch.setattr(ak, "is_valid_api_key", lambda k: False)

    async def fake_cu(_request):
        return {"id": 9, "role": "Admin"}

    monkeypatch.setattr(deps, "current_user", fake_cu)
    assert await deps.require_admin_or_api_key(_Req()) == "session:9"


async def test_no_credential_401(monkeypatch):
    monkeypatch.setattr(ak, "_dev_mode", False)
    monkeypatch.setattr(ak, "is_valid_api_key", lambda k: False)

    async def fake_cu(_request):
        return None

    monkeypatch.setattr(deps, "current_user", fake_cu)
    with pytest.raises(HTTPException) as ei:
        await deps.require_admin_or_api_key(_Req())
    assert ei.value.status_code == 401
