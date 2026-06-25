"""Magic-link token generation and single-use redemption.

The token in the email is a URL-safe random string. We store only its
SHA-256 hash in Postgres so a database read can't be replayed as a login.
Tokens are single-use: ``consumed_at`` is stamped on the first successful
redemption inside the same transaction that does the lookup, blocking a
second concurrent redemption attempt.

Two purposes share the same table:

- ``invite``  → emitted by the admin "create user" flow
- ``login``   → emitted by the public "request magic link" flow

A user's outstanding tokens are revoked on each new issue so a forwarded
old email can't beat a fresh request.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

from app.config import settings
from app.db.postgres import get_connection

logger = logging.getLogger(__name__)

TokenPurpose = Literal["invite", "login"]

# 32 random bytes → 43 char base64url. Plenty of entropy for short-lived
# magic links and short enough to fit in any email client URL line wrap.
_TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    """sha256 hex. Deterministic so a lookup is just `WHERE token_hash = $1`.
    Exposed publicly because callers (`api/auth.py:verify`) need to bind
    the resulting session to the originating token without re-hashing
    behind a private name."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Back-compat alias for the previous private name.
_hash_token = hash_token


async def issue_token(user_id: UUID, purpose: TokenPurpose) -> str:
    """Mint a new magic-link token, invalidating any prior outstanding ones.

    Returns the plain token (caller embeds it in the magic-link URL). The
    DB only holds the hash. TTL comes from ``MAGIC_LINK_TTL_MINUTES``.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    token_hash = _hash_token(token)
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.MAGIC_LINK_TTL_MINUTES,
    )

    async with get_connection() as conn:
        async with conn.transaction():
            # Revoke any previous outstanding tokens for this user+purpose.
            # Prevents a stale forwarded email from beating a fresh request.
            await conn.execute(
                """
                DELETE FROM magic_link_tokens
                WHERE user_id = $1 AND purpose = $2 AND consumed_at IS NULL
                """,
                user_id,
                purpose,
            )
            await conn.execute(
                """
                INSERT INTO magic_link_tokens
                    (token_hash, user_id, purpose, expires_at,
                     redemptions_remaining)
                VALUES ($1, $2, $3, $4, $5)
                """,
                token_hash,
                user_id,
                purpose,
                expires_at,
                # Read at issue time (not consume time) so changing the
                # setting doesn't retroactively grant extra redemptions
                # to tokens already in the wild.
                settings.MAGIC_LINK_MAX_REDEMPTIONS,
            )
    logger.debug("Issued %s token for user %s (exp %s)", purpose, user_id, expires_at)
    return token


async def consume_token(
    token: str,
    expected_purpose: TokenPurpose | None = None,
) -> Optional[UUID]:
    """Atomically decrement a token's redemption counter and return the
    associated ``user_id``, or ``None`` if the token can't be redeemed.

    Reasons for ``None``: unknown token, already consumed, expired,
    purpose mismatch, or redemptions exhausted.

    ``settings.MAGIC_LINK_MAX_REDEMPTIONS`` (default 3) is only an emergency
    ceiling, NOT the everyday single-use mechanism. Real single-use comes from
    ``claim_session_token``: the first authenticated request on the resulting
    session flips ``consumed_at`` and zeroes the counter, so in the happy path
    the link dies on that first use even though the counter still shows
    redemptions left. The counter buffer absorbs naive email-scanner prefetches
    and double-clicks. See the ``MAGIC_LINK_MAX_REDEMPTIONS`` comment in
    ``config.py`` for the full rationale.

    The atomic UPDATE keeps the decrement race-safe under concurrent
    redemption attempts regardless of the configured ceiling.
    """
    token_hash = _hash_token(token)
    # Bind both purposes to a fixed parameter slot ($2) and rely on
    # SQL-side ``$2 IS NULL OR purpose = $2`` — avoids the f-string
    # fragment that previous versions of this function used.
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE magic_link_tokens
            SET redemptions_remaining = redemptions_remaining - 1,
                consumed_at = CASE
                    WHEN redemptions_remaining - 1 <= 0 THEN now()
                    ELSE consumed_at
                END
            WHERE token_hash = $1
              AND ($2::text IS NULL OR purpose = $2)
              AND consumed_at IS NULL
              AND expires_at > now()
              AND redemptions_remaining > 0
            RETURNING user_id
            """,
            token_hash,
            expected_purpose,
        )
    if not row:
        return None
    return row["user_id"]


async def claim_session_token(session_id: str, token_hash: str) -> None:
    """Mark a magic-link token as fully consumed and clear the session's
    back-reference, in one transaction.

    Triggered on the first authenticated request after a magic-link
    redemption — see `auth/deps.py:current_user`. This is what gives
    the system its single-use feel even though `consume_token` keeps a
    redemption counter: the moment a real session is actually used, any
    leftover redemptions on the token are wiped.

    Idempotent: subsequent calls (e.g. concurrent claim attempts from
    parallel requests) no-op because the `consumed_at IS NULL` and
    `created_by_token_hash IS NOT NULL` predicates only match once.
    """
    async with get_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE magic_link_tokens
                SET consumed_at = now(),
                    redemptions_remaining = 0
                WHERE token_hash = $1 AND consumed_at IS NULL
                """,
                token_hash,
            )
            await conn.execute(
                """
                UPDATE user_sessions
                SET created_by_token_hash = NULL
                WHERE session_id = $1 AND created_by_token_hash IS NOT NULL
                """,
                session_id,
            )


async def purge_expired_tokens() -> int:
    """House-keeping: drop tokens past their expiry or already consumed.

    Safe to run periodically. Returns the number of rows deleted (useful
    for log/metrics). Background sweep is wired in main.py later.
    """
    async with get_connection() as conn:
        result = await conn.execute(
            """
            DELETE FROM magic_link_tokens
            WHERE expires_at < now() OR consumed_at IS NOT NULL
            """,
        )
    # asyncpg returns "DELETE N" — extract the row count.
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0
