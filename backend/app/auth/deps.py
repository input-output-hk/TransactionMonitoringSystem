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

    Side effect: if the session row still carries a
    ``created_by_token_hash`` (i.e. this is the FIRST authenticated
    request after a magic-link redemption), claim this session: delete
    any other session still linked to the same token and clear this
    one's back-reference. The originating magic link itself is left
    alone — it can still be redeemed again up to
    ``MAGIC_LINK_MAX_REDEMPTIONS`` — see ``tokens.claim_session_token``.

    Never raises — endpoints that want a hard requirement should chain
    on top with :func:`require_user` or :func:`require_admin`.
    """
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not session_id:
        return None
    user = await lookup_session(session_id)
    if user and user.get("created_by_token_hash"):
        # Imported lazily because tokens.py imports from app.db.postgres
        # which already imports from app.config — keeping the import
        # tree flat helps `python -m app.cli` cold-start.
        from app.auth.tokens import claim_session_token
        await claim_session_token(
            session_id=user["session_id"],
            token_hash=user["created_by_token_hash"],
        )
        # Reflect the post-claim state in the dict we return, so a
        # caller that inspects this field sees the cleared value.
        user["created_by_token_hash"] = None
    return user


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


async def require_admin_or_api_key(request: Request) -> str:
    """Allow a valid API key (a trusted programmatic operator credential) or
    dev-mode, otherwise require an Admin SESSION. Crucially, a non-admin
    (Reviewer) session is REJECTED. Used to gate state-changing / expensive
    operations that a Reviewer must not run (e.g. the clustering proxy's
    DELETE-contract and heavy-job mutations, which ``verify_api_key`` alone
    would let any session perform). Returns a principal string for auditing.
    """
    # Imported lazily to keep the auth import tree flat (api_key imports config
    # only; deps must not create a cycle through it at module load).
    from app.auth.api_key import _dev_mode, is_valid_api_key

    if _dev_mode:
        return "dev-mode"
    key = request.headers.get(settings.API_KEY_HEADER)
    if key and is_valid_api_key(key):
        return key  # raw key; audit.actor_from_principal fingerprints it
    user = await current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Cookie"},
        )
    if user.get("role") != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required.",
        )
    return f"session:{user['id']}"
