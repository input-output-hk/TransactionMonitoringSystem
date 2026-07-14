"""Unit tests for the magic-link token lifecycle (app/auth/tokens.py) and
the session-claim side effect in deps.current_user.

These run against a faked Postgres connection, so they pin the CALL
CONTRACT: which statements run, in what order, with which parameters, and
how command tags and rows are interpreted. The WHERE-clause semantics that
actually distinguish fresh / expired / exhausted / already-consumed tokens
live in Postgres and are exercised for real in
tests/live_db/test_auth_lifecycle_pg.py.
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.auth.deps as deps
import app.auth.tokens as tokens
from app.auth.tokens import (
    claim_session_token,
    consume_token,
    hash_token,
    issue_token,
    purge_expired_tokens,
)
from app.config import settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
def conn(monkeypatch):
    """Fake asyncpg connection wired into app.auth.tokens.get_connection."""
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock(return_value="DELETE 0")
    mock_conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def fake_transaction():
        yield

    mock_conn.transaction = fake_transaction

    @asynccontextmanager
    async def fake_get_connection():
        yield mock_conn

    # tokens.py binds get_connection at import time; patch its binding.
    monkeypatch.setattr(tokens, "get_connection", fake_get_connection)
    return mock_conn


class TestConsumeToken:
    async def test_unredeemable_returns_none(self, conn):
        conn.fetchrow.return_value = None
        assert await consume_token("x" * 43) is None

    async def test_redeemable_returns_user_id(self, conn):
        user_id = uuid.uuid4()
        conn.fetchrow.return_value = {"user_id": user_id}
        assert await consume_token("x" * 43) == user_id

    async def test_passes_hash_not_plain_token(self, conn):
        token = "x" * 43
        await consume_token(token, expected_purpose="invite")
        args = conn.fetchrow.await_args.args
        assert args[1] == hash_token(token)
        assert token not in args  # the plain token must never reach SQL
        assert args[2] == "invite"

    async def test_no_purpose_binds_null(self, conn):
        # The verify route redeems without a purpose so invite and login
        # links go through the same path; NULL disables the purpose guard.
        await consume_token("x" * 43)
        assert conn.fetchrow.await_args.args[2] is None

    async def test_update_carries_all_lifecycle_guards(self, conn):
        # The single atomic UPDATE is the whole not-redeemable logic;
        # losing any one guard silently widens token validity.
        await consume_token("x" * 43)
        sql = conn.fetchrow.await_args.args[0]
        assert "consumed_at IS NULL" in sql
        assert "expires_at > now()" in sql
        assert "redemptions_remaining > 0" in sql
        assert "redemptions_remaining - 1" in sql
        assert "RETURNING user_id" in sql


class TestIssueToken:
    async def test_returns_plain_token_stores_hash(self, conn):
        user_id = uuid.uuid4()
        token = await issue_token(user_id, "login")
        insert_call = conn.execute.await_args_list[-1]
        assert hash_token(token) == insert_call.args[1]
        assert token != insert_call.args[1]

    async def test_revokes_prior_outstanding_tokens_first(self, conn):
        user_id = uuid.uuid4()
        await issue_token(user_id, "invite")
        delete_call, insert_call = conn.execute.await_args_list
        assert "DELETE FROM magic_link_tokens" in delete_call.args[0]
        assert "consumed_at IS NULL" in delete_call.args[0]
        assert delete_call.args[1:] == (user_id, "invite")
        assert "INSERT INTO magic_link_tokens" in insert_call.args[0]

    async def test_redemption_budget_baked_in_at_issue_time(self, conn):
        await issue_token(uuid.uuid4(), "login")
        insert_call = conn.execute.await_args_list[-1]
        assert insert_call.args[5] == settings.MAGIC_LINK_MAX_REDEMPTIONS


class TestClaimSessionToken:
    async def test_returns_sibling_revocation_count(self, conn):
        conn.execute.side_effect = ["SELECT 1", "DELETE 2", "UPDATE 1"]
        revoked = await claim_session_token("sess-a", "hash-a")
        assert revoked == 2

    async def test_lock_delete_clear_sequence(self, conn):
        conn.execute.side_effect = ["SELECT 1", "DELETE 0", "UPDATE 1"]
        await claim_session_token("sess-a", "hash-a")
        lock, delete, clear = conn.execute.await_args_list
        # Lock the token row first so concurrent claims serialize.
        assert "FOR UPDATE" in lock.args[0]
        assert lock.args[1] == "hash-a"
        # Kill only OTHER sessions minted from the same token.
        assert "session_id <> $2" in delete.args[0]
        assert delete.args[1:] == ("hash-a", "sess-a")
        # Clear our own back-reference so we are excluded from any
        # later sibling DELETE.
        assert "created_by_token_hash = NULL" in clear.args[0]
        assert clear.args[1] == "sess-a"

    async def test_unparseable_command_tag_degrades_to_zero(self, conn):
        conn.execute.side_effect = ["SELECT 1", "", "UPDATE 1"]
        assert await claim_session_token("sess-a", "hash-a") == 0


class TestPurgeExpiredTokens:
    async def test_returns_deleted_count(self, conn):
        conn.execute.return_value = "DELETE 7"
        assert await purge_expired_tokens() == 7

    async def test_targets_expired_and_consumed(self, conn):
        await purge_expired_tokens()
        sql = conn.execute.await_args.args[0]
        assert "expires_at < now()" in sql
        assert "consumed_at IS NOT NULL" in sql


class _Request:
    """Minimal stand-in for starlette.Request: cookies only."""

    def __init__(self, cookies=None):
        self.cookies = cookies or {}


class TestCurrentUserClaimSideEffect:
    """The session claim (TOCTOU cleanup) fires on the FIRST authenticated
    request after redemption, inside deps.current_user."""

    async def test_first_request_claims_and_clears_backref(self, monkeypatch):
        user = {
            "id": uuid.uuid4(),
            "role": "Reviewer",
            "session_id": "sess-a",
            "created_by_token_hash": "hash-a",
        }
        monkeypatch.setattr(deps, "lookup_session", AsyncMock(return_value=dict(user)))
        claim = AsyncMock(return_value=0)
        # current_user imports claim_session_token lazily from app.auth.tokens.
        monkeypatch.setattr(tokens, "claim_session_token", claim)

        request = _Request({settings.SESSION_COOKIE_NAME: "sess-a"})
        resolved = await deps.current_user(request)

        claim.assert_awaited_once_with(session_id="sess-a", token_hash="hash-a")
        assert resolved["created_by_token_hash"] is None

    async def test_already_claimed_session_skips_claim(self, monkeypatch):
        user = {
            "id": uuid.uuid4(),
            "role": "Reviewer",
            "session_id": "sess-a",
            "created_by_token_hash": None,
        }
        monkeypatch.setattr(deps, "lookup_session", AsyncMock(return_value=dict(user)))
        claim = AsyncMock()
        monkeypatch.setattr(tokens, "claim_session_token", claim)

        request = _Request({settings.SESSION_COOKIE_NAME: "sess-a"})
        resolved = await deps.current_user(request)

        claim.assert_not_awaited()
        assert resolved["session_id"] == "sess-a"

    async def test_no_cookie_returns_none_without_lookup(self, monkeypatch):
        lookup = AsyncMock()
        monkeypatch.setattr(deps, "lookup_session", lookup)
        assert await deps.current_user(_Request()) is None
        lookup.assert_not_awaited()
