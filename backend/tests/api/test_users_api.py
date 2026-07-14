"""HTTP tests for the admin user-management router (app/api/users.py).

Covers the full Admin CRUD surface plus the two auth failure classes
(anonymous 401, non-admin 403) and the route guardrails: self-delete,
last-active-Admin delete, resend-invite only for pending users.
Postgres is faked at the get_connection seam; the email side effect is
stubbed at _issue_invite_email (which is best-effort by design).
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import app.api.users as users_api
from app.auth.deps import current_user, require_admin

ADMIN_ID = uuid.uuid4()


def _user_row(role="Reviewer", status="active", user_id=None, email=None):
    user_id = user_id or uuid.uuid4()
    return {
        "id": user_id,
        "email": email or f"user-{user_id.hex[:8]}@example.com",
        "full_name": "Test User",
        "role": role,
        "status": status,
        "created_at": datetime.now(timezone.utc),
        "last_login_at": None,
    }


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def as_admin():
    from app.main import app

    # The id must be a real UUID: delete_user compares it to the path UUID.
    admin = {"email": "admin@example.com", "role": "Admin", "id": ADMIN_ID}
    app.dependency_overrides[require_admin] = lambda: admin
    yield admin
    app.dependency_overrides.pop(require_admin, None)


@pytest.fixture
def conn(monkeypatch):
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock(return_value="DELETE 1")
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value=0)

    @asynccontextmanager
    async def fake_get_connection():
        yield mock_conn

    monkeypatch.setattr(users_api, "get_connection", fake_get_connection)
    return mock_conn


@pytest.fixture
def invite_email(monkeypatch):
    stub = AsyncMock()
    monkeypatch.setattr(users_api, "_issue_invite_email", stub)
    return stub


class TestListUsers:
    def test_paginated_shape(self, client, as_admin, conn):
        conn.fetch.return_value = [_user_row(), _user_row(role="Admin")]
        conn.fetchval.return_value = 7

        r = client.get("/api/users")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 2
        assert body["total"] == 7
        assert {u["role"] for u in body["data"]} == {"Reviewer", "Admin"}

    def test_limit_bounds_rejected(self, client, as_admin, conn):
        assert client.get("/api/users?limit=0").status_code == 422
        assert client.get("/api/users?limit=1001").status_code == 422


class TestCreateUser:
    def test_created_and_invited(self, client, as_admin, conn, invite_email):
        created = _user_row(status="pending", email="new@example.com")
        # The duplicate probe is a fetchval (fixture default 0 = no
        # duplicate); the INSERT ... RETURNING row is the fetchrow.
        conn.fetchrow.return_value = created

        r = client.post(
            "/api/users",
            json={
                "email": "new@example.com",
                "full_name": "New Person",
                "role": "Reviewer",
            },
        )

        assert r.status_code == 201, r.text
        assert r.json()["email"] == "new@example.com"
        assert r.json()["status"] == "pending"
        invite_email.assert_awaited_once()

    def test_duplicate_email_conflict(self, client, as_admin, conn, invite_email):
        conn.fetchval.return_value = 1  # duplicate probe hit

        r = client.post(
            "/api/users",
            json={
                "email": "dup@example.com",
                "full_name": "Dup",
                "role": "Reviewer",
            },
        )

        assert r.status_code == 409
        invite_email.assert_not_awaited()

    @pytest.mark.parametrize(
        "payload",
        [
            {"email": "not-an-email", "full_name": "X", "role": "Reviewer"},
            {"email": "ok@example.com", "full_name": "", "role": "Reviewer"},
            {"email": "ok@example.com", "full_name": "X", "role": "Root"},
            {"full_name": "X", "role": "Reviewer"},
        ],
        ids=["bad-email", "blank-name", "bad-role", "missing-email"],
    )
    def test_invalid_payload_rejected(self, client, as_admin, conn, payload):
        assert client.post("/api/users", json=payload).status_code == 422


class TestDeleteUser:
    def test_cannot_delete_self(self, client, as_admin, conn):
        r = client.delete(f"/api/users/{ADMIN_ID}")
        assert r.status_code == 400
        assert "own account" in r.json()["detail"]

    def test_missing_user_404(self, client, as_admin, conn):
        conn.fetchrow.return_value = None
        assert client.delete(f"/api/users/{uuid.uuid4()}").status_code == 404

    def test_cannot_delete_last_active_admin(self, client, as_admin, conn):
        conn.fetchrow.return_value = {"role": "Admin", "status": "active"}
        conn.fetchval.return_value = 0

        r = client.delete(f"/api/users/{uuid.uuid4()}")

        assert r.status_code == 400
        assert "last active Admin" in r.json()["detail"]

    def test_admin_deletable_when_another_remains(self, client, as_admin, conn, monkeypatch):
        conn.fetchrow.return_value = {"role": "Admin", "status": "active"}
        conn.fetchval.return_value = 1
        revoke = AsyncMock(return_value=2)
        monkeypatch.setattr(users_api, "delete_all_sessions_for_user", revoke)

        r = client.delete(f"/api/users/{uuid.uuid4()}")

        assert r.status_code == 204
        revoke.assert_awaited_once()

    def test_reviewer_deleted_with_sessions_revoked(self, client, as_admin, conn, monkeypatch):
        conn.fetchrow.return_value = {"role": "Reviewer", "status": "active"}
        revoke = AsyncMock(return_value=0)
        monkeypatch.setattr(users_api, "delete_all_sessions_for_user", revoke)

        target = uuid.uuid4()
        r = client.delete(f"/api/users/{target}")

        assert r.status_code == 204
        delete_sql, delete_arg = conn.execute.await_args.args
        assert "DELETE FROM users" in delete_sql
        assert delete_arg == target


class TestResendInvite:
    def test_missing_user_404(self, client, as_admin, conn, invite_email):
        conn.fetchrow.return_value = None
        r = client.post(f"/api/users/{uuid.uuid4()}/resend-invite")
        assert r.status_code == 404
        invite_email.assert_not_awaited()

    def test_active_user_rejected(self, client, as_admin, conn, invite_email):
        conn.fetchrow.return_value = {
            "email": "a@example.com",
            "full_name": "A",
            "status": "active",
        }
        r = client.post(f"/api/users/{uuid.uuid4()}/resend-invite")
        assert r.status_code == 400
        invite_email.assert_not_awaited()

    def test_pending_user_reinvited(self, client, as_admin, conn, invite_email):
        conn.fetchrow.return_value = {
            "email": "p@example.com",
            "full_name": "P",
            "status": "pending",
        }
        r = client.post(f"/api/users/{uuid.uuid4()}/resend-invite")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        invite_email.assert_awaited_once()


class TestAccessControl:
    """Run the REAL require_admin chain: override only the upstream
    current_user so the role check itself is exercised."""

    @pytest.fixture
    def as_reviewer(self):
        from app.main import app

        reviewer = {
            "email": "rev@example.com",
            "role": "Reviewer",
            "id": uuid.uuid4(),
        }

        async def _reviewer(request=None):
            return reviewer

        app.dependency_overrides[current_user] = _reviewer
        yield reviewer
        app.dependency_overrides.pop(current_user, None)

    def test_reviewer_forbidden(self, client, as_reviewer, conn):
        assert client.get("/api/users").status_code == 403
        assert client.delete(f"/api/users/{uuid.uuid4()}").status_code == 403

    def test_anonymous_unauthorized(self, client, conn):
        assert client.get("/api/users").status_code == 401
        r = client.post(
            "/api/users",
            json={
                "email": "x@example.com",
                "full_name": "X",
                "role": "Reviewer",
            },
        )
        assert r.status_code == 401
