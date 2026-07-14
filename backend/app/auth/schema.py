"""Postgres schema for magic-link authentication.

Three tables:

- ``users`` — one row per human user. Replaces the legacy stub table that
  carried ``username``/``password_hash`` columns (unused by any endpoint).
- ``magic_link_tokens`` — one-shot tokens for login + invite flows. Stored
  hashed so a DB compromise doesn't leak active links.
- ``user_sessions`` — opaque session IDs issued after a successful magic
  link redemption. The session ID lives in an HTTP-only cookie.

The migration is idempotent: we detect the legacy schema by the presence
of a ``password_hash`` column and DROP CASCADE before recreating, so a
fresh dev box gets the new schema in one go and existing dev boxes get
migrated on first restart. Subsequent restarts no-op via ``IF NOT EXISTS``.
"""

from __future__ import annotations

import logging

from app.db.postgres import get_connection

logger = logging.getLogger(__name__)


async def execute_auth_schema() -> None:
    """Create / migrate the auth tables. Safe to run on every startup."""
    async with get_connection() as conn:
        # ── Legacy migration ────────────────────────────────────────────
        legacy = await conn.fetchval(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'users'
              AND column_name = 'password_hash'
            LIMIT 1
            """,
        )
        if legacy:
            logger.warning(
                "Dropping legacy users table (password_hash schema). Magic-link auth replaces it.",
            )
            await conn.execute("DROP TABLE IF EXISTS users CASCADE")

        # ── users ───────────────────────────────────────────────────────
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            UUID PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                full_name     TEXT NOT NULL,
                role          TEXT NOT NULL
                              CHECK (role IN ('Admin','Reviewer')),
                status        TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','active','disabled')),
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_login_at TIMESTAMPTZ
            )
            """,
        )
        # Case-insensitive lookup on email so "Foo@Bar.com" === "foo@bar.com".
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx
            ON users (lower(email))
            """,
        )

        # ── magic_link_tokens ───────────────────────────────────────────
        # token_hash = sha256 of the user-facing token. We never store the
        # plain token, so a DB read can't be used to log in as anyone.
        #
        # `redemptions_remaining` lets a token survive a few "drive-by"
        # GETs from email security scanners (Defender, Gmail, Mimecast)
        # before being marked consumed. Initial value is set per-token
        # by issue_token (read from MAGIC_LINK_MAX_REDEMPTIONS settings).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS magic_link_tokens (
                token_hash             TEXT PRIMARY KEY,
                user_id                UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                purpose                TEXT NOT NULL
                                       CHECK (purpose IN ('invite','login')),
                expires_at             TIMESTAMPTZ NOT NULL,
                consumed_at            TIMESTAMPTZ,
                redemptions_remaining  INT NOT NULL DEFAULT 1,
                created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
        )
        # Idempotent ALTER for installs that pre-date the redemption-count
        # column. Safe to run on every startup — Postgres no-ops when the
        # column already exists.
        await conn.execute(
            """
            ALTER TABLE magic_link_tokens
            ADD COLUMN IF NOT EXISTS redemptions_remaining INT NOT NULL DEFAULT 1
            """,
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS magic_link_tokens_user_idx
            ON magic_link_tokens (user_id)
            """,
        )
        # Background cleanup will scan by expires_at; an index keeps it cheap.
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS magic_link_tokens_expires_idx
            ON magic_link_tokens (expires_at)
            """,
        )

        # ── user_sessions ───────────────────────────────────────────────
        # session_id is 32 random bytes hex-encoded. NOT a JWT — opaque so
        # admin "disable user" can revoke instantly by deleting rows.
        #
        # `created_by_token_hash` back-references the magic-link token
        # that established this session. The first authenticated request
        # that comes with a valid session AND a still-set back-reference
        # claims the SESSION (clears the column and deletes any other
        # session row still linked to the same token) — see
        # `tokens.claim_session_token`. It deliberately does NOT kill the
        # token itself: the link can still be redeemed again up to
        # MAGIC_LINK_MAX_REDEMPTIONS, so a second honest open of the same
        # email link keeps working instead of dying the instant the first
        # one is used.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                session_id              TEXT PRIMARY KEY,
                user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at              TIMESTAMPTZ NOT NULL,
                user_agent              TEXT,
                ip                      TEXT,
                created_by_token_hash   TEXT
            )
            """,
        )
        # Idempotent ALTER for installs that pre-date the back-reference
        # column. No FK on purpose: tokens get purged regularly, and we
        # don't want session lookup to fail if the token row was already
        # garbage-collected after consumption.
        await conn.execute(
            """
            ALTER TABLE user_sessions
            ADD COLUMN IF NOT EXISTS created_by_token_hash TEXT
            """,
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS user_sessions_user_idx
            ON user_sessions (user_id)
            """,
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS user_sessions_expires_idx
            ON user_sessions (expires_at)
            """,
        )
        # Powers claim_session_token's sibling-revocation DELETE (WHERE
        # created_by_token_hash = $1). Partial: the column goes NULL once
        # a session claims or loses the race, so most rows never qualify —
        # indexing only the live back-references keeps it small.
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS user_sessions_created_by_token_hash_idx
            ON user_sessions (created_by_token_hash)
            WHERE created_by_token_hash IS NOT NULL
            """,
        )

        logger.info("Auth schema ready (users, magic_link_tokens, user_sessions)")
