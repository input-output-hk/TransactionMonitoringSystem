"""Opaque session IDs backed by a Postgres row.

Why opaque (not JWT): the admin "delete user" / "disable user" flow needs
instant revocation, and we already have Postgres available. A row lookup
adds ~0.5 ms and lets us revoke a session by simply DELETE-ing the row.
JWT would force a refresh-token dance + denylist for the same outcome.

The session ID lives in an HTTP-only cookie named ``tms_session``. The
SPA never reads or writes it; the browser includes it automatically on
same-origin requests.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.config import settings
from app.db.postgres import get_connection

logger = logging.getLogger(__name__)

# 32 bytes → 43 base64url chars. Roughly the same entropy as a UUIDv4
# but cheaper to compare as a primary key.
_SESSION_BYTES = 32


async def create_session(
    user_id: UUID,
    user_agent: str | None = None,
    ip: str | None = None,
    token_hash: str | None = None,
) -> tuple[str, datetime]:
    """Mint a session for ``user_id`` and bump the user's ``last_login_at``.

    Returns ``(session_id, expires_at)``. Caller is responsible for
    setting the HTTP-only cookie. ``last_login_at`` is updated in the
    same transaction so audit views are always consistent with the
    sessions table.

    ``token_hash``: if this session is being created from a magic-link
    redemption, pass the originating token's sha256 hash so the first
    authenticated request can claim (forcibly consume) the token even
    when its redemption counter still has slack. See
    `tokens.claim_session_token`.
    """
    session_id = secrets.token_urlsafe(_SESSION_BYTES)
    expires_at = datetime.now(UTC) + timedelta(
        days=settings.SESSION_TTL_DAYS,
    )

    async with get_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO user_sessions
                    (session_id, user_id, expires_at, user_agent, ip,
                     created_by_token_hash)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                session_id,
                user_id,
                expires_at,
                user_agent,
                ip,
                token_hash,
            )
            await conn.execute(
                """
                UPDATE users
                SET last_login_at = now(),
                    status = CASE WHEN status = 'pending' THEN 'active' ELSE status END
                WHERE id = $1
                """,
                user_id,
            )
    logger.debug("Created session for user %s (exp %s)", user_id, expires_at)
    return session_id, expires_at


async def lookup_session(session_id: str) -> dict | None:
    """Resolve a session ID to a user dict, or None if invalid/expired.

    The returned dict contains the full user row plus ``session_id`` and
    ``session_expires_at`` for the caller's convenience. Returns None
    for any of: missing session, expired session, disabled user.
    """
    if not session_id:
        return None
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                u.id, u.email, u.full_name, u.role, u.status,
                u.created_at, u.last_login_at,
                s.session_id, s.expires_at AS session_expires_at,
                s.created_by_token_hash
            FROM user_sessions AS s
            JOIN users AS u ON u.id = s.user_id
            WHERE s.session_id = $1
              AND s.expires_at > now()
              AND u.status <> 'disabled'
            """,
            session_id,
        )
    if not row:
        return None
    return dict(row)


async def delete_session(session_id: str) -> None:
    """Hard-delete a single session. Used by logout."""
    if not session_id:
        return
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM user_sessions WHERE session_id = $1",
            session_id,
        )


async def delete_all_sessions_for_user(user_id: UUID) -> int:
    """Revoke every session for a user — used when an admin disables or
    deletes them. Returns the number of sessions revoked."""
    async with get_connection() as conn:
        result = await conn.execute(
            "DELETE FROM user_sessions WHERE user_id = $1",
            user_id,
        )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def purge_expired_sessions() -> int:
    """House-keeping: drop sessions past expiry."""
    async with get_connection() as conn:
        result = await conn.execute(
            "DELETE FROM user_sessions WHERE expires_at < now()",
        )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0
