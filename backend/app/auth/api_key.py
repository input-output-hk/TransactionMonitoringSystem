"""API Key + session-cookie authentication for FastAPI.

This module unifies two parallel auth mechanisms behind a single
``verify_api_key`` dependency so existing endpoints don't need to change:

- **API key** via the ``TMS-API-Key`` header — for server-to-server
  callers (CLI, integrations, the analysis engine talking to itself).
- **Session cookie** via ``settings.SESSION_COOKIE_NAME`` (default
  ``tms_session``) — for browser users authenticated through the
  magic-link flow.

Either credential unlocks the same endpoints. Routes that need *human*
auth specifically (e.g. ``/api/users``) should use ``require_user`` or
``require_admin`` from :mod:`app.auth.deps` instead.
"""

import hmac
import logging
from typing import List, Optional

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.config import settings

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(
    name=settings.API_KEY_HEADER,
    auto_error=False,
    description="API key for authentication",
)

# Parse and cache valid keys once at startup — avoids repeated string splits on every request
_valid_keys: List[str] = (
    [k.strip() for k in settings.API_KEYS.split(",") if k.strip()] if settings.API_KEYS else []
)
_dev_mode: bool = not _valid_keys
# Dev-mode warning is emitted from main.py lifespan so that logging is fully
# configured before the message is written.


def is_valid_api_key(candidate: Optional[str]) -> bool:
    """Constant-time check against the configured API keys.

    Returns True when ``candidate`` matches any key in ``_valid_keys``. The
    loop intentionally runs to completion (no early ``break``) so the total
    comparison time is independent of which key matched, closing the timing
    side-channel that simple ``in``-comparison leaks.

    Returns False in dev mode as well — callers that want to allow dev-mode
    traffic must check ``_dev_mode`` separately so the policy is explicit.
    """
    if not candidate or not _valid_keys:
        return False
    matched = False
    for k in _valid_keys:
        if hmac.compare_digest(candidate, k):
            matched = True
    return matched


async def verify_api_key(
    request: Request,
    api_key: Optional[str] = Security(api_key_header),
) -> str:
    """Accept either a valid API key OR a valid session cookie.

    Returns a short string identifying the credential used:
      - ``"dev-mode"`` when no API_KEYS are configured (dev fallback)
      - the raw API key string when ``TMS-API-Key`` matched
      - ``"session:<user_id>"`` when a session cookie resolved to a user

    Raises 401 only if neither credential is present/valid: the caller is
    unauthenticated (RFC 9110 semantics), which is what lets the SPA's
    global UnauthorizedError handler distinguish "session expired, go to
    login" from a true 403 "authenticated but not allowed" (require_admin).
    Importing ``lookup_session`` lazily here avoids a circular dependency:
    ``app.auth.sessions`` already imports from ``app.db.postgres`` which
    doesn't touch this module.
    """
    if _dev_mode:
        return "dev-mode"

    # 1) API key wins if present and valid — preserves the historical
    #    behaviour for server-to-server callers.
    if api_key and is_valid_api_key(api_key):
        return api_key

    # 2) Otherwise try the session cookie. Imported lazily because the
    #    sessions module needs the Postgres pool to be initialized.
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if session_id:
        from app.auth.sessions import lookup_session  # local to dodge cycles

        user = await lookup_session(session_id)
        if user:
            # Same session-claim as `deps.current_user`. Lives in both
            # call sites so any authenticated request reaching endpoints
            # that still go through `verify_api_key` (the bulk of the
            # existing API) also triggers sibling-session cleanup. See
            # `tokens.claim_session_token`.
            if user.get("created_by_token_hash"):
                from app.auth.tokens import claim_session_token

                await claim_session_token(
                    session_id=user["session_id"],
                    token_hash=user["created_by_token_hash"],
                )
            return f"session:{user['id']}"

    # No credential matched: 401 (unauthenticated), matching require_user,
    # so an expired browser session triggers the SPA's login redirect on
    # every protected endpoint, not only the session-specific routes. The
    # message intentionally stays neutral so an attacker probing different
    # surfaces can't tell whether the endpoint expected a key vs a session.
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
        # RFC 9110: a 401 carries WWW-Authenticate naming the scheme. The
        # custom scheme name doubles as documentation and never triggers a
        # browser basic-auth prompt.
        headers={"WWW-Authenticate": settings.API_KEY_HEADER},
    )
