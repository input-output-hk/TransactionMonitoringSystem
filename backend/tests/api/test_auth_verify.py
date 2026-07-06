"""TestClient coverage for the magic-link redemption route
GET /api/auth/verify (app/api/auth.py).

Pins the route contract:

- every not-redeemable outcome (unknown, expired, exhausted, consumed)
  maps to ONE generic 400 so the endpoint is not an oracle for probing
  token validity,
- a disabled or vanished user cannot redeem even a valid token, with the
  same indistinguishable 400,
- success mints a session bound to the token hash, sets the HTTP-only
  cookie, and returns the POST-login user state (pending flips to active).

The SQL-side distinctions between the failure modes are covered against
real Postgres in tests/live_db/test_auth_lifecycle_pg.py.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import app.api.auth as auth_api
from app.auth.tokens import hash_token
from app.config import settings

# Length of secrets.token_urlsafe(32), the shape a real link carries.
VALID_TOKEN = "t" * 43

GENERIC_DETAIL = "This link is invalid or has expired."


def _user_row(status="active", user_id=None):
    return {
        "id": user_id or uuid.uuid4(),
        "email": "operator@example.com",
        "full_name": "Op Erator",
        "role": "Reviewer",
        "status": status,
        "created_at": datetime.now(timezone.utc),
        "last_login_at": None,
    }


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def session_stub(monkeypatch):
    expires = datetime.now(timezone.utc) + timedelta(
        days=settings.SESSION_TTL_DAYS
    )
    stub = AsyncMock(return_value=("sess-abc", expires))
    monkeypatch.setattr(auth_api, "create_session", stub)
    return stub


class TestVerifySuccess:
    def test_sets_http_only_session_cookie(
        self, client, monkeypatch, session_stub
    ):
        user_id = uuid.uuid4()
        monkeypatch.setattr(
            auth_api, "consume_token", AsyncMock(return_value=user_id)
        )
        monkeypatch.setattr(
            auth_api,
            "_get_user",
            AsyncMock(return_value=_user_row(user_id=user_id)),
        )

        r = client.get("/api/auth/verify", params={"token": VALID_TOKEN})

        assert r.status_code == 200, r.text
        set_cookie = r.headers["set-cookie"].lower()
        assert f"{settings.SESSION_COOKIE_NAME}=sess-abc" in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        body = r.json()
        assert body["email"] == "operator@example.com"
        assert body["role"] == "Reviewer"

    def test_session_bound_to_token_hash(
        self, client, monkeypatch, session_stub
    ):
        # The session row must carry the token hash so the first
        # authenticated request can revoke sibling sessions minted from
        # the same link (claim_session_token).
        user_id = uuid.uuid4()
        monkeypatch.setattr(
            auth_api, "consume_token", AsyncMock(return_value=user_id)
        )
        monkeypatch.setattr(
            auth_api,
            "_get_user",
            AsyncMock(return_value=_user_row(user_id=user_id)),
        )

        client.get("/api/auth/verify", params={"token": VALID_TOKEN})

        assert (
            session_stub.await_args.kwargs["token_hash"]
            == hash_token(VALID_TOKEN)
        )
        assert session_stub.await_args.kwargs["user_id"] == user_id

    def test_pending_user_returned_as_active(
        self, client, monkeypatch, session_stub
    ):
        # create_session flips pending to active in the DB; the route
        # re-fetches the user afterwards so the response reflects the
        # post-login state, not the pre-login snapshot.
        user_id = uuid.uuid4()
        monkeypatch.setattr(
            auth_api, "consume_token", AsyncMock(return_value=user_id)
        )
        get_user = AsyncMock(
            side_effect=[
                _user_row(status="pending", user_id=user_id),
                _user_row(status="active", user_id=user_id),
            ]
        )
        monkeypatch.setattr(auth_api, "_get_user", get_user)

        r = client.get("/api/auth/verify", params={"token": VALID_TOKEN})

        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"
        assert get_user.await_count == 2


class TestVerifyRejection:
    def test_unredeemable_token_gets_generic_400(
        self, client, monkeypatch, session_stub
    ):
        # consume_token folds unknown/expired/exhausted/consumed into one
        # None; the route must not re-differentiate them.
        monkeypatch.setattr(
            auth_api, "consume_token", AsyncMock(return_value=None)
        )

        r = client.get("/api/auth/verify", params={"token": VALID_TOKEN})

        assert r.status_code == 400
        assert r.json()["detail"] == GENERIC_DETAIL
        session_stub.assert_not_awaited()
        assert "set-cookie" not in r.headers

    @pytest.mark.parametrize("user_state", ["disabled", "vanished"])
    def test_disabled_or_vanished_user_gets_same_400(
        self, client, monkeypatch, session_stub, user_state
    ):
        # A valid token for a dead account must be indistinguishable from
        # a bad token: no user-state oracle, no session.
        user_id = uuid.uuid4()
        monkeypatch.setattr(
            auth_api, "consume_token", AsyncMock(return_value=user_id)
        )
        row = None if user_state == "vanished" else _user_row(status="disabled")
        monkeypatch.setattr(
            auth_api, "_get_user", AsyncMock(return_value=row)
        )

        r = client.get("/api/auth/verify", params={"token": VALID_TOKEN})

        assert r.status_code == 400
        assert r.json()["detail"] == GENERIC_DETAIL
        session_stub.assert_not_awaited()
        assert "set-cookie" not in r.headers


class TestVerifyQueryValidation:
    @pytest.mark.parametrize(
        "params",
        [{}, {"token": "short"}, {"token": "x" * 201}],
        ids=["missing", "too-short", "too-long"],
    )
    def test_malformed_token_rejected_before_handler(
        self, client, monkeypatch, params
    ):
        consume = AsyncMock()
        monkeypatch.setattr(auth_api, "consume_token", consume)

        r = client.get("/api/auth/verify", params=params)

        assert r.status_code == 422
        consume.assert_not_awaited()
