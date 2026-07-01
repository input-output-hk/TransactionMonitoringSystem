"""Magic-link token generation and bounded-redemption consumption.

The token in the email is a URL-safe random string. We store only its
SHA-256 hash in Postgres so a database read can't be replayed as a login.
Each token allows up to ``MAGIC_LINK_MAX_REDEMPTIONS`` redemptions (default
3) before ``consume_token`` refuses it — that budget is what absorbs a
few "drive-by" GETs (an email security scanner, a chat-app link unfurl,
the same person opening the link twice) without the link dying before its
intended use. ``claim_session_token`` separately cleans up *sessions*
spawned by those drive-by redemptions once one of them is actually used;
see its docstring for why that's a session-level concern rather than a
reason to kill the token early.

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

    ``settings.MAGIC_LINK_MAX_REDEMPTIONS`` (default 3) IS the single-use
    budget — this decrement is the only thing that ever kills a token
    (besides expiry). An earlier revision also had ``claim_session_token``
    zero the counter on the first authenticated request, meant to give a
    tighter "single-use feel"; that back-fired on the ordinary case of a
    user opening the same email link twice (slow first load, a second
    click "just in case", the same link on a second device) — the first
    open succeeded and silently killed the link before the second open
    ran, which then saw "invalid or expired" for no real reason. Now the
    budget alone decides, so a second honest open of the same link keeps
    working as long as redemptions remain. See the
    ``MAGIC_LINK_MAX_REDEMPTIONS`` comment in ``config.py`` for the full
    rationale, and ``claim_session_token`` for the complementary
    session-cleanup job.

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


async def claim_session_token(session_id: str, token_hash: str) -> int:
    """Claim this SESSION on behalf of a magic-link redemption: delete any
    other session still linked to the same token, then clear this one's
    own back-reference.
    """
    async with get_connection() as conn:
        async with conn.transaction():
            # Pure lock acquisition (see "Serialization" above) — forces
            # concurrent claims for this token to run one at a time.
            await conn.execute(
                "SELECT 1 FROM magic_link_tokens WHERE token_hash = $1 FOR UPDATE",
                token_hash,
            )
            # Kill every OTHER session minted from this token that hasn't
            # claimed itself yet. Sessions that already ran this function
            # have a NULL created_by_token_hash and so are naturally
            # excluded from this DELETE.
            result = await conn.execute(
                """
                DELETE FROM user_sessions
                WHERE created_by_token_hash = $1 AND session_id <> $2
                """,
                token_hash,
                session_id,
            )
            await conn.execute(
                """
                UPDATE user_sessions
                SET created_by_token_hash = NULL
                WHERE session_id = $1 AND created_by_token_hash IS NOT NULL
                """,
                session_id,
            )
    try:
        revoked = int(result.split()[-1])
    except (ValueError, IndexError):
        revoked = 0
    if revoked:
        logger.warning(
            "claim_session_token: revoked %d sibling session(s) for a "
            "magic-link token (hash prefix %s…) — the link was evidently "
            "redeemed by more than one party",
            revoked, token_hash[:12],
        )
    return revoked


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
