"""Real-Postgres coverage of the magic-link auth lifecycle.

The fresh / expired / exhausted / already-consumed distinctions of
consume_token live in one SQL WHERE clause, and the TOCTOU sibling
cleanup of claim_session_token lives in FOR UPDATE + DELETE semantics.
Mocks cannot regress-test either, so this is the authoritative coverage;
tests/api/v1/test_auth_tokens_sessions.py only pins the call contract.

Requires TMS_LIVE_DB_TESTS=1 (see conftest).
"""

import uuid

from app.auth.sessions import create_session, lookup_session
from app.auth.tokens import (
    claim_session_token,
    consume_token,
    hash_token,
    issue_token,
    purge_expired_tokens,
)
from app.config import settings
from app.db.postgres import get_connection


async def _mk_user(status: str = "active") -> uuid.UUID:
    user_id = uuid.uuid4()
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, email, full_name, role, status)
            VALUES ($1, $2, $3, $4, $5)
            """,
            user_id,
            f"live-{user_id.hex}@example.test",
            "Live DB Test",
            "Reviewer",
            status,
        )
    return user_id


async def _rm_user(user_id: uuid.UUID) -> None:
    # Tokens and sessions cascade with the user row.
    async with get_connection() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


async def _expire_tokens(user_id: uuid.UUID) -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE magic_link_tokens
            SET expires_at = now() - interval '1 minute'
            WHERE user_id = $1
            """,
            user_id,
        )


class TestConsumeTokenSemantics:
    def test_budget_then_exhaustion(self, pg_run):
        async def scenario():
            user_id = await _mk_user()
            try:
                token = await issue_token(user_id, "login")
                # The configured budget IS the single-use policy; every
                # redemption within it must succeed, the next must not.
                for _ in range(settings.MAGIC_LINK_MAX_REDEMPTIONS):
                    assert await consume_token(token) == user_id
                assert await consume_token(token) is None
                # Exhaustion also stamps consumed_at, making the row
                # eligible for the purge sweep.
                async with get_connection() as conn:
                    consumed_at = await conn.fetchval(
                        "SELECT consumed_at FROM magic_link_tokens WHERE token_hash = $1",
                        hash_token(token),
                    )
                assert consumed_at is not None
            finally:
                await _rm_user(user_id)

        pg_run(scenario)

    def test_expired_token_not_redeemable(self, pg_run):
        async def scenario():
            user_id = await _mk_user()
            try:
                token = await issue_token(user_id, "login")
                await _expire_tokens(user_id)
                assert await consume_token(token) is None
            finally:
                await _rm_user(user_id)

        pg_run(scenario)

    def test_purpose_mismatch_neither_redeems_nor_burns(self, pg_run):
        async def scenario():
            user_id = await _mk_user()
            try:
                token = await issue_token(user_id, "invite")
                assert await consume_token(token, "login") is None
                # The mismatch must not have decremented the budget.
                assert await consume_token(token, "invite") == user_id
            finally:
                await _rm_user(user_id)

        pg_run(scenario)

    def test_reissue_revokes_prior_outstanding_token(self, pg_run):
        async def scenario():
            user_id = await _mk_user()
            try:
                stale = await issue_token(user_id, "login")
                fresh = await issue_token(user_id, "login")
                assert await consume_token(stale) is None
                assert await consume_token(fresh) == user_id
            finally:
                await _rm_user(user_id)

        pg_run(scenario)

    def test_unknown_token_not_redeemable(self, pg_run):
        async def scenario():
            assert await consume_token("never-issued-" + "x" * 30) is None

        pg_run(scenario)

    def test_purge_drops_expired_rows(self, pg_run):
        async def scenario():
            user_id = await _mk_user()
            try:
                await issue_token(user_id, "login")
                await _expire_tokens(user_id)
                assert await purge_expired_tokens() >= 1
            finally:
                await _rm_user(user_id)

        pg_run(scenario)


class TestSessionClaimToctou:
    def test_claim_revokes_sibling_sessions(self, pg_run):
        async def scenario():
            user_id = await _mk_user()
            try:
                token = await issue_token(user_id, "login")
                token_hash = hash_token(token)
                # Two parties redeem the same link before either makes an
                # authenticated request.
                first, _ = await create_session(user_id, token_hash=token_hash)
                second, _ = await create_session(user_id, token_hash=token_hash)

                revoked = await claim_session_token(session_id=first, token_hash=token_hash)

                assert revoked == 1
                assert await lookup_session(second) is None
                claimed = await lookup_session(first)
                assert claimed is not None
                assert claimed["created_by_token_hash"] is None
            finally:
                await _rm_user(user_id)

        pg_run(scenario)

    def test_claimed_session_survives_later_claims(self, pg_run):
        async def scenario():
            user_id = await _mk_user()
            try:
                token = await issue_token(user_id, "login")
                token_hash = hash_token(token)
                first, _ = await create_session(user_id, token_hash=token_hash)
                await claim_session_token(session_id=first, token_hash=token_hash)
                # A later redemption of the same link claims its own
                # session; the already-claimed one has a NULL back-ref and
                # must not be caught in the sibling DELETE.
                third, _ = await create_session(user_id, token_hash=token_hash)
                revoked = await claim_session_token(session_id=third, token_hash=token_hash)
                assert revoked == 0
                assert await lookup_session(first) is not None
                assert await lookup_session(third) is not None
            finally:
                await _rm_user(user_id)

        pg_run(scenario)


class TestSessionUserGuards:
    def test_disabled_user_sessions_go_dark(self, pg_run):
        async def scenario():
            user_id = await _mk_user()
            try:
                session_id, _ = await create_session(user_id)
                assert await lookup_session(session_id) is not None
                async with get_connection() as conn:
                    await conn.execute(
                        "UPDATE users SET status = 'disabled' WHERE id = $1",
                        user_id,
                    )
                # Disabling must revoke access instantly, without waiting
                # for session expiry.
                assert await lookup_session(session_id) is None
            finally:
                await _rm_user(user_id)

        pg_run(scenario)

    def test_first_login_flips_pending_to_active(self, pg_run):
        async def scenario():
            user_id = await _mk_user(status="pending")
            try:
                session_id, _ = await create_session(user_id)
                user = await lookup_session(session_id)
                assert user is not None
                assert user["status"] == "active"
                assert user["last_login_at"] is not None
            finally:
                await _rm_user(user_id)

        pg_run(scenario)
