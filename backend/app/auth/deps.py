"""FastAPI dependencies for session-cookie auth.

Phase 2 wires the actual `/api/auth/*` endpoints on top of these. They
live here in Phase 1 so the building blocks are testable in isolation
before the routes exist.

Cookie strategy: the session ID rides in an HTTP-only cookie named
``SESSION_COOKIE_NAME`` (default ``tms_session``). The browser includes
it automatically on same-origin requests; the SPA never reads it.

Two dependency shapes:

- ``current_user`` — returns the resolved user dict or ``None`` for
  unauthenticated requests. Use when the endpoint is mixed-mode (e.g.
  also accessible via API key).
- ``require_user`` / ``require_admin`` — raise ``401`` / ``403``
  appropriately. Use when the route is human-only.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from app.auth.sessions import lookup_session
from app.config import settings

logger = logging.getLogger(__name__)


async def current_user(request: Request) -> Optional[dict]:
    """Resolve the session cookie to a user dict, or ``None`` if absent.

    Never raises — endpoints that want a hard requirement should chain
    on top with :func:`require_user` or :func:`require_admin`.
    """
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return await lookup_session(session_id)


async def require_user(
    user: Optional[dict] = Depends(current_user),
) -> dict:
    """401 if no valid session. Returns the user dict otherwise."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return user


async def require_admin(
    user: dict = Depends(require_user),
) -> dict:
    """403 if the user isn't an Admin. Returns the user dict otherwise."""
    if user.get("role") != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required.",
        )
    return user
