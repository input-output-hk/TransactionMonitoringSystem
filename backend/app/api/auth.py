"""Magic-link auth endpoints.

Public routes:

- ``POST /api/auth/request-link`` — mints a login link, emails it. Always
  returns 200 to avoid leaking which addresses correspond to real users.
- ``GET /api/auth/verify?token=…`` — exchanges a one-shot magic link for
  a session cookie. Used by both login and invite flows.

Authenticated routes (session cookie):

- ``POST /api/auth/logout`` — invalidates the current session.
- ``GET /api/auth/me`` — returns the current user.

Cookie shape: opaque ``tms_session`` ID, HTTP-only, ``SameSite=Lax``,
``Secure`` when the inbound request looks HTTPS (auto-detected via the
URL scheme or ``X-Forwarded-Proto`` so dev http://localhost still works).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.auth.deps import require_user
from app.auth.email import send_magic_link
from app.auth.models import RequestLinkPayload, User
from app.auth.sessions import create_session, delete_session
from app.auth.tokens import consume_token, issue_token
from app.config import settings
from app.db.postgres import get_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Helpers ─────────────────────────────────────────────────────────────


def _is_secure_request(request: Request) -> bool:
    """Decide whether to mark the session cookie ``Secure``.

    Direct HTTPS request → yes. Behind Cloudflare Tunnel / a reverse proxy
    terminating TLS → check ``X-Forwarded-Proto``. Plain http://localhost
    in dev → no (browsers reject `Secure` cookies on insecure origins).
    """
    if request.url.scheme == "https":
        return True
    fwd = request.headers.get("x-forwarded-proto", "").lower()
    return fwd == "https"


def _set_session_cookie(
    request: Request, response: Response, session_id: str,
) -> None:
    """Apply the session cookie to ``response`` with the right flags."""
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session_id,
        max_age=settings.SESSION_TTL_DAYS * 86_400,
        path="/",
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
    )


def _clear_session_cookie(request: Request, response: Response) -> None:
    """Tell the browser to drop the session cookie."""
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
    )


async def _find_active_user_by_email(email: str) -> Optional[dict]:
    """Case-insensitive lookup. Returns None for non-existent OR disabled."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, full_name, role, status, created_at, last_login_at
            FROM users
            WHERE lower(email) = lower($1) AND status <> 'disabled'
            LIMIT 1
            """,
            email,
        )
    return dict(row) if row else None


async def _get_user(user_id) -> Optional[dict]:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, full_name, role, status, created_at, last_login_at
            FROM users
            WHERE id = $1
            """,
            user_id,
        )
    return dict(row) if row else None


# ── Routes ──────────────────────────────────────────────────────────────


@router.post("/request-link", status_code=status.HTTP_200_OK)
async def request_link(payload: RequestLinkPayload):
    """Send a magic-link email if the address matches an active user.

    Returns 200 unconditionally. The same response shape is returned for
    both existing and unknown emails so an attacker can't enumerate users
    by probing this endpoint. Token issuance + SMTP send happens
    in-process; failures are logged, never surfaced to the caller.
    """
    user = await _find_active_user_by_email(payload.email)
    if user is not None:
        try:
            token = await issue_token(user["id"], "login")
            await send_magic_link(
                to_email=user["email"],
                full_name=user["full_name"],
                token=token,
                purpose="login",
            )
        except Exception as e:
            # Catch-all: do NOT propagate. Logging is the audit trail.
            logger.error(
                "request-link: token/email issuance failed for %s: %s",
                payload.email, e,
            )
    else:
        # Log only — never surface to client.
        logger.info(
            "request-link: no active user for %s (silent 200)", payload.email,
        )
    return {"status": "ok"}


@router.get("/verify")
async def verify(
    request: Request,
    response: Response,
    token: str = Query(..., min_length=20, max_length=200),
):
    """Redeem a one-shot magic-link token and start a session.

    On success: sets the HTTP-only session cookie and returns the user.
    On failure (invalid / expired / consumed): 400 with a generic detail
    — the frontend just shows "this link is no longer valid".
    """
    user_id = await consume_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This link is invalid or has expired.",
        )

    user = await _get_user(user_id)
    if user is None or user["status"] == "disabled":
        # User vanished between token issue and redemption — defensive.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This link is invalid or has expired.",
        )

    # Trim UA at 500 chars — some bots send absurdly long strings.
    user_agent = (request.headers.get("user-agent") or "")[:500] or None
    client_ip = request.client.host if request.client else None

    session_id, _ = await create_session(
        user_id=user["id"],
        user_agent=user_agent,
        ip=client_ip,
    )
    _set_session_cookie(request, response, session_id)

    # create_session bumps status pending→active; re-fetch so the returned
    # user reflects that.
    fresh = await _get_user(user["id"])
    return User(**(fresh or user)).model_dump(mode="json")


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Drop the current session. Idempotent: works even with no cookie."""
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if session_id:
        await delete_session(session_id)
    _clear_session_cookie(request, response)
    return {"status": "ok"}


@router.get("/me", response_model=User)
async def me(user: dict = Depends(require_user)):
    """Return the currently authenticated user."""
    return User(**user)
