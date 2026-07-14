"""Admin user-management endpoints.

All routes here are gated by :func:`app.auth.deps.require_admin` — API keys
alone are NOT sufficient, because there's no role concept attached to an
API key. Browser sessions with ``role='Admin'`` are the only credential
that unlocks these endpoints.

Endpoints:

- ``GET    /api/users``                       — list (most-recent first)
- ``POST   /api/users``                       — create + send invite
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
from app.auth.models import User, UserCreate
from app.auth.sessions import delete_all_sessions_for_user
from app.auth.tokens import issue_token
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
       the system un-administrable.
    """
    if user_id == admin["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account.",
        )

    async with get_connection() as conn:
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
                    detail="Cannot delete the last active Admin.",
                )

        # CASCADE drops their tokens + sessions, but call explicit
        # session-revoke first so we get an accurate revoked-count for the
        # audit log without parsing asyncpg result strings.
        revoked = await delete_all_sessions_for_user(user_id)
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
