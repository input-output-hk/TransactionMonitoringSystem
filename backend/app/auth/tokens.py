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


def _hash_token(token: str) -> str:
    """sha256 hex. Deterministic so a lookup is just `WHERE token_hash = $1`."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
                    (token_hash, user_id, purpose, expires_at)
                VALUES ($1, $2, $3, $4)
                """,
                token_hash,
                user_id,
                purpose,
                expires_at,
            )
    logger.debug("Issued %s token for user %s (exp %s)", purpose, user_id, expires_at)
    return token


async def consume_token(
    token: str,
    expected_purpose: TokenPurpose | None = None,
) -> Optional[UUID]:
    """Atomically validate a magic-link token and mark it consumed.

    Returns the ``user_id`` on success. Returns ``None`` if the token is
    unknown, already consumed, expired, or doesn't match the requested
    purpose. The UPDATE … WHERE clause ensures only one caller can
    succeed for a given token even under concurrent redemption attempts.
    """
    token_hash = _hash_token(token)
    purpose_check = "AND purpose = $2" if expected_purpose else ""
    args = [token_hash]
    if expected_purpose:
        args.append(expected_purpose)

    async with get_connection() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE magic_link_tokens
            SET consumed_at = now()
            WHERE token_hash = $1
              {purpose_check}
              AND consumed_at IS NULL
              AND expires_at > now()
            RETURNING user_id
            """,
            *args,
        )
    if not row:
        return None
    return row["user_id"]


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
