"""Admin user-management endpoints.

All routes here are gated by :func:`app.auth.deps.require_admin` — API keys
alone are NOT sufficient, because there's no role concept attached to an
API key. Browser sessions with ``role='Admin'`` are the only credential
that unlocks these endpoints.

Endpoints:

- ``GET    /api/users``                       — list (most-recent first)
- ``POST   /api/users``                       — create + send invite
- ``PATCH  /api/users/{id}``                  — change role (Reviewer ⇄ Admin)
- ``DELETE /api/users/{id}``                  — remove user + revoke sessions
- ``POST   /api/users/{id}/resend-invite``    — regenerate token + email
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.api._params import PageLimit, PageOffset
from app.auth.deps import require_admin
from app.auth.email import send_magic_link
from app.auth.models import User, UserCreate, UserUpdate
from app.auth.sessions import delete_all_sessions_for_user
from app.auth.tokens import issue_token
from app.config import settings
from app.db.postgres import get_connection
from app.models.common import ListResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


_USER_COLUMNS = "id, email, full_name, role, status, created_at, last_login_at"


# ── helpers ─────────────────────────────────────────────────────────────


async def _issue_invite_email(
    user_id: UUID,
    email: str,
    full_name: str,
) -> None:
    """Mint an invite token and best-effort send the magic link.

    Errors are logged, never re-raised — the admin shouldn't see a 500
    because Mailpit is down or the SMTP provider is flaky. The frontend
    can offer "resend invite" if delivery fails.
    """
    try:
        token = await issue_token(user_id, "invite")
        await send_magic_link(
            to_email=email,
            full_name=full_name,
            token=token,
            purpose="invite",
        )
    except Exception as e:
        logger.error(
            "invite issuance failed for user %s (%s): %s",
            user_id,
            email,
            e,
        )


async def _lock_admin_invariant(conn) -> None:
    """Serialize every mutation that can reduce the active-Admin count.

    Must be called inside a transaction (``conn.transaction()``). The
    advisory lock is transaction-scoped, so it auto-releases on commit or
    rollback. Because ``update_user`` (demotion) and ``delete_user`` both
    take the SAME key before reading the "other active admins" count, the
    count-then-write guard can no longer race itself: without this, two
    concurrent demotions/deletions each observe another active Admin and
    both commit, leaving zero Admins.
    """
    await conn.execute("SELECT pg_advisory_xact_lock($1)", settings.ADMIN_INVARIANT_LOCK_KEY)


async def _assert_admin_remains(conn, user_id: UUID, action: str) -> None:
    """Raise 400 if stripping this user's Admin privilege would leave the
    system with no active Admin. Caller must already hold the admin-invariant
    lock (see :func:`_lock_admin_invariant`) so the count is race-free.

    ``action`` is the verb for the error message ("demote" / "delete")."""
    other_active_admins = await conn.fetchval(
        """
        SELECT count(*) FROM users
        WHERE role = 'Admin'
          AND status <> 'disabled'
          AND id <> $1
        """,
        user_id,
    )
    if other_active_admins == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot {action} the last active Admin.",
        )


# ── routes ──────────────────────────────────────────────────────────────


@router.get("", response_model=ListResponse[User])
async def list_users(
    _admin: dict = Depends(require_admin),
    limit: PageLimit = 100,
    offset: PageOffset = 0,
):
    """Return paginated users, newest first.

    Response shape matches the other listing endpoints (``/api/v1/archive``,
    ``/api/v1/analysis/results``): ``{count, total, data}`` so the frontend
    paginator can show "Total Users: N" alongside the current page.
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_USER_COLUMNS}
            FROM users
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        total = await conn.fetchval("SELECT count(*) FROM users")
    data = [User(**dict(r)) for r in rows]
    return {"count": len(data), "total": int(total or 0), "data": data}


@router.post(
    "",
    response_model=User,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    payload: UserCreate,
    _admin: dict = Depends(require_admin),
) -> User:
    """Create a new user in ``pending`` state and email them an invite link.

    Duplicate emails are rejected with 409 — the admin UI should surface
    this as "this address is already invited / a member".
    """
    async with get_connection() as conn:
        existing = await conn.fetchval(
            "SELECT 1 FROM users WHERE lower(email) = lower($1)",
            payload.email,
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this email already exists.",
            )

        user_id = uuid.uuid4()
        row = await conn.fetchrow(
            f"""
            INSERT INTO users (id, email, full_name, role, status)
            VALUES ($1, $2, $3, $4, 'pending')
            RETURNING {_USER_COLUMNS}
            """,
            user_id,
            payload.email,
            payload.full_name,
            payload.role,
        )

    await _issue_invite_email(user_id, payload.email, payload.full_name)
    return User(**dict(row))


@router.patch("/{user_id}", response_model=User)
async def update_user(
    payload: UserUpdate,
    user_id: UUID = Path(...),
    admin: dict = Depends(require_admin),
) -> User:
    """Change a user's role (Reviewer ⇄ Admin).

    Guardrails mirror :func:`delete_user`, because a mis-applied role change
    can render the system un-administrable just like a deletion:

    1. An admin can't change their own role — demoting yourself would strip
       admin access from the in-flight session; blocking it outright also
       avoids the surprising "promote myself" no-op.
    2. The last remaining active Admin can't be demoted to Reviewer — would
       leave no one able to administer the system. The check and the write
       run under the admin-invariant advisory lock inside one transaction,
       so concurrent demotions can't both slip past it.
    """
    if user_id == admin["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot change your own role.",
        )

    async with get_connection() as conn, conn.transaction():
        await _lock_admin_invariant(conn)

        target = await conn.fetchrow(
            "SELECT role, status FROM users WHERE id = $1",
            user_id,
        )
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )

        if target["role"] == "Admin" and payload.role == "Reviewer":
            await _assert_admin_remains(conn, user_id, "demote")

        row = await conn.fetchrow(
            f"""
            UPDATE users SET role = $1 WHERE id = $2
            RETURNING {_USER_COLUMNS}
            """,
            payload.role,
            user_id,
        )
        if row is None:
            # The row passed the existence check above but was deleted before
            # the UPDATE (concurrent delete). Report it as gone rather than
            # letting User(**dict(None)) raise an opaque 500.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )
        logger.info("Changed role of user %s to %s", user_id, payload.role)

    return User(**dict(row))


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID = Path(...),
    admin: dict = Depends(require_admin),
):
    """Remove a user and revoke all their sessions.

    Two guardrails:

    1. An admin can't delete their own account (would log themselves out
       and likely break the in-flight UI).
    2. The last remaining active Admin can't be deleted — would render
       the system un-administrable. The check and the delete run under the
       admin-invariant advisory lock inside one transaction, so a delete
       racing another delete/demotion can't strand the system.
    """
    if user_id == admin["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account.",
        )

    async with get_connection() as conn, conn.transaction():
        await _lock_admin_invariant(conn)

        target = await conn.fetchrow(
            "SELECT role, status FROM users WHERE id = $1",
            user_id,
        )
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )

        if target["role"] == "Admin":
            await _assert_admin_remains(conn, user_id, "delete")

        # CASCADE drops their tokens + sessions, but call explicit
        # session-revoke first so we get an accurate revoked-count for the
        # audit log without parsing asyncpg result strings. Reuse THIS
        # connection (not a second pooled one) so the revoke joins this
        # advisory-locked transaction and can't exhaust the pool: see
        # delete_all_sessions_for_user.
        revoked = await delete_all_sessions_for_user(user_id, conn)
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        logger.info(
            "Deleted user %s (revoked %d active sessions)",
            user_id,
            revoked,
        )


@router.post("/{user_id}/resend-invite", status_code=status.HTTP_200_OK)
async def resend_invite(
    user_id: UUID = Path(...),
    _admin: dict = Depends(require_admin),
):
    """Regenerate the invite token and resend the magic-link email.

    Only valid for users still in ``pending`` state — once they've logged
    in once the status flips to ``active`` and re-sending an invite link
    would be misleading.
    """
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT email, full_name, status FROM users WHERE id = $1
            """,
            user_id,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )
    if row["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has already activated their account.",
        )

    await _issue_invite_email(user_id, row["email"], row["full_name"])
    return {"status": "ok"}
