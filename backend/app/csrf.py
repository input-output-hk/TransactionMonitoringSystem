"""CSRF double-submit cookie check (defense-in-depth).

The session cookie is ``SameSite=Lax``, which already blocks a cross-site
POST/PUT/PATCH/DELETE from carrying it (browsers only attach a Lax cookie to
a cross-site top-level GET navigation). This middleware adds a second,
independent control: a mutating request that carries the session cookie must
also echo a JS-readable CSRF cookie's value in a request header. A cross-site
attacker's page cannot read this origin's cookies (same-origin policy), so it
cannot produce a matching header even if it can make the browser send the
session cookie.

The CSRF cookie itself is set/cleared alongside the session cookie in
app.api.auth (_set_session_cookie / _clear_session_cookie).
"""

import hmac
import logging

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger(__name__)

# Only these methods mutate state; GET/HEAD/OPTIONS never need a CSRF check.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class CSRFMiddleware(BaseHTTPMiddleware):
    """Reject a cookie-authed mutating request whose CSRF header does not
    match its CSRF cookie.

    Gated on the SESSION cookie being present, not on whether the route
    actually requires auth: a request with no session cookie is not
    cookie-authed (e.g. an API-key caller, or the pre-login
    ``/api/auth/request-link``), so it is out of scope for this check —
    downstream auth dependencies still apply as normal.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.CSRF_PROTECTION_ENABLED:
            return await call_next(request)
        if request.method not in _UNSAFE_METHODS:
            return await call_next(request)
        session_cookie = request.cookies.get(settings.SESSION_COOKIE_NAME)
        if not session_cookie:
            return await call_next(request)

        cookie_token = request.cookies.get(settings.CSRF_COOKIE_NAME, "")
        header_token = request.headers.get(settings.CSRF_HEADER_NAME, "")
        # Constant-time compare: guards against a timing oracle on the token
        # match, consistent with the webhook HMAC comparison elsewhere.
        if not cookie_token or not hmac.compare_digest(cookie_token, header_token):
            logger.warning(
                "CSRF check failed for %s %s (missing or mismatched %s)",
                request.method, request.url.path, settings.CSRF_HEADER_NAME,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token missing or invalid."},
            )
        return await call_next(request)
