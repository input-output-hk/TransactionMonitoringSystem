"""HTTP tests for the admin user-management router (app/api/v1/users.py).

Covers the full Admin CRUD surface plus the two auth failure classes
(anonymous 401, non-admin 403) and the route guardrails: self-delete,
last-active-Admin delete, resend-invite only for pending users.
Postgres is faked at the get_connection seam; the email side effect is
stubbed at _issue_invite_email (which is best-effort by design).
"""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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
        "created_at": datetime.now(UTC),
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

    # update_user / delete_user run inside `conn.transaction()`; give the
    # mock a real async-CM so `async with ... conn.transaction():` works.
    # A fresh CM per call, since each request opens its own transaction.
    @asynccontextmanager
    async def _txn():
        yield mock_conn

    mock_conn.transaction = lambda *a, **k: _txn()

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

        r = client.get("/api/v1/users")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 2
        assert body["total"] == 7
        assert {u["role"] for u in body["data"]} == {"Reviewer", "Admin"}

    def test_limit_bounds_rejected(self, client, as_admin, conn):
        assert client.get("/api/v1/users?limit=0").status_code == 422
        assert client.get("/api/v1/users?limit=1001").status_code == 422


class TestCreateUser:
    def test_created_and_invited(self, client, as_admin, conn, invite_email):
        created = _user_row(status="pending", email="new@example.com")
        # The duplicate probe is a fetchval (fixture default 0 = no
        # duplicate); the INSERT ... RETURNING row is the fetchrow.
        conn.fetchrow.return_value = created

        r = client.post(
            "/api/v1/users",
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
            "/api/v1/users",
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
        assert client.post("/api/v1/users", json=payload).status_code == 422


class TestUpdateUser:
    def test_promote_reviewer_to_admin(self, client, as_admin, conn):
        target_id = uuid.uuid4()
        # First fetchrow = the current row (Reviewer); second = the
        # UPDATE ... RETURNING row (now Admin).
        conn.fetchrow.side_effect = [
            {"role": "Reviewer", "status": "active"},
            _user_row(role="Admin", user_id=target_id),
        ]

        r = client.patch(f"/api/v1/users/{target_id}", json={"role": "Admin"})

        assert r.status_code == 200, r.text
        assert r.json()["role"] == "Admin"

    def test_demote_admin_to_reviewer(self, client, as_admin, conn):
        target_id = uuid.uuid4()
        conn.fetchrow.side_effect = [
            {"role": "Admin", "status": "active"},
            _user_row(role="Reviewer", user_id=target_id),
        ]
        conn.fetchval.return_value = 1  # another active Admin remains

        r = client.patch(f"/api/v1/users/{target_id}", json={"role": "Reviewer"})

        assert r.status_code == 200, r.text
        assert r.json()["role"] == "Reviewer"

    def test_cannot_change_own_role(self, client, as_admin, conn):
        r = client.patch(f"/api/v1/users/{ADMIN_ID}", json={"role": "Reviewer"})
        assert r.status_code == 400
        assert "own role" in r.json()["detail"]

    def test_missing_user_404(self, client, as_admin, conn):
        conn.fetchrow.return_value = None
        r = client.patch(f"/api/v1/users/{uuid.uuid4()}", json={"role": "Admin"})
        assert r.status_code == 404

    def test_deleted_between_select_and_update_404(self, client, as_admin, conn):
        # Row exists at the existence check but a concurrent delete removes
        # it before the UPDATE ... RETURNING, which then matches no row.
        conn.fetchrow.side_effect = [
            {"role": "Reviewer", "status": "active"},
            None,
        ]
        r = client.patch(f"/api/v1/users/{uuid.uuid4()}", json={"role": "Admin"})
        assert r.status_code == 404

    def test_cannot_demote_last_active_admin(self, client, as_admin, conn):
        conn.fetchrow.return_value = {"role": "Admin", "status": "active"}
        conn.fetchval.return_value = 0  # no other active Admin

        r = client.patch(f"/api/v1/users/{uuid.uuid4()}", json={"role": "Reviewer"})

        assert r.status_code == 400
        assert "last active Admin" in r.json()["detail"]

    def test_invalid_role_rejected(self, client, as_admin, conn):
        r = client.patch(f"/api/v1/users/{uuid.uuid4()}", json={"role": "Root"})
        assert r.status_code == 422

    def test_takes_admin_invariant_lock(self, client, as_admin, conn):
        # The last-Admin guard is only safe if the check+write are serialized;
        # assert the transaction-scoped advisory lock is actually acquired.
        conn.fetchrow.side_effect = [
            {"role": "Admin", "status": "active"},
            _user_row(role="Reviewer"),
        ]
        conn.fetchval.return_value = 1

        client.patch(f"/api/v1/users/{uuid.uuid4()}", json={"role": "Reviewer"})

        assert any(
            "pg_advisory_xact_lock" in call.args[0] for call in conn.execute.await_args_list
        ), "expected the admin-invariant advisory lock to be taken"


class TestDeleteUser:
    def test_cannot_delete_self(self, client, as_admin, conn):
        r = client.delete(f"/api/v1/users/{ADMIN_ID}")
        assert r.status_code == 400
        assert "own account" in r.json()["detail"]

    def test_missing_user_404(self, client, as_admin, conn):
        conn.fetchrow.return_value = None
        assert client.delete(f"/api/v1/users/{uuid.uuid4()}").status_code == 404

    def test_cannot_delete_last_active_admin(self, client, as_admin, conn):
        conn.fetchrow.return_value = {"role": "Admin", "status": "active"}
        conn.fetchval.return_value = 0

        r = client.delete(f"/api/v1/users/{uuid.uuid4()}")

        assert r.status_code == 400
        assert "last active Admin" in r.json()["detail"]

    def test_admin_deletable_when_another_remains(self, client, as_admin, conn, monkeypatch):
        conn.fetchrow.return_value = {"role": "Admin", "status": "active"}
        conn.fetchval.return_value = 1
        revoke = AsyncMock(return_value=2)
        monkeypatch.setattr(users_api, "delete_all_sessions_for_user", revoke)

        r = client.delete(f"/api/v1/users/{uuid.uuid4()}")

        assert r.status_code == 204
        revoke.assert_awaited_once()

    def test_reviewer_deleted_with_sessions_revoked(self, client, as_admin, conn, monkeypatch):
        conn.fetchrow.return_value = {"role": "Reviewer", "status": "active"}
        revoke = AsyncMock(return_value=0)
        monkeypatch.setattr(users_api, "delete_all_sessions_for_user", revoke)

        target = uuid.uuid4()
        r = client.delete(f"/api/v1/users/{target}")

        assert r.status_code == 204
        delete_sql, delete_arg = conn.execute.await_args.args
        assert "DELETE FROM users" in delete_sql
        assert delete_arg == target

    def test_takes_lock_and_reuses_conn_for_revoke(self, client, as_admin, conn, monkeypatch):
        # Delete must (a) take the admin-invariant advisory lock and (b) revoke
        # sessions on THIS transaction's connection, not a second pooled one
        # (a nested acquire under the lock can exhaust the pool and deadlock).
        conn.fetchrow.return_value = {"role": "Reviewer", "status": "active"}
        revoke = AsyncMock(return_value=0)
        monkeypatch.setattr(users_api, "delete_all_sessions_for_user", revoke)

        target = uuid.uuid4()
        r = client.delete(f"/api/v1/users/{target}")

        assert r.status_code == 204
        assert any(
            "pg_advisory_xact_lock" in call.args[0] for call in conn.execute.await_args_list
        ), "expected the admin-invariant advisory lock on the delete path"
        assert revoke.await_args.args == (target, conn)


class TestResendInvite:
    def test_missing_user_404(self, client, as_admin, conn, invite_email):
        conn.fetchrow.return_value = None
        r = client.post(f"/api/v1/users/{uuid.uuid4()}/resend-invite")
        assert r.status_code == 404
        invite_email.assert_not_awaited()

    def test_active_user_rejected(self, client, as_admin, conn, invite_email):
        conn.fetchrow.return_value = {
            "email": "a@example.com",
            "full_name": "A",
            "status": "active",
        }
        r = client.post(f"/api/v1/users/{uuid.uuid4()}/resend-invite")
        assert r.status_code == 400
        invite_email.assert_not_awaited()

    def test_pending_user_reinvited(self, client, as_admin, conn, invite_email):
        conn.fetchrow.return_value = {
            "email": "p@example.com",
            "full_name": "P",
            "status": "pending",
        }
        r = client.post(f"/api/v1/users/{uuid.uuid4()}/resend-invite")
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
        assert client.get("/api/v1/users").status_code == 403
        assert client.delete(f"/api/v1/users/{uuid.uuid4()}").status_code == 403
        r = client.patch(f"/api/v1/users/{uuid.uuid4()}", json={"role": "Admin"})
        assert r.status_code == 403

    def test_anonymous_unauthorized(self, client, conn):
        assert client.get("/api/v1/users").status_code == 401
        r = client.post(
            "/api/v1/users",
            json={
                "email": "x@example.com",
                "full_name": "X",
                "role": "Reviewer",
            },
        )
        assert r.status_code == 401
        patch = client.patch(f"/api/v1/users/{uuid.uuid4()}", json={"role": "Admin"})
        assert patch.status_code == 401
