"""In-memory sliding window rate limiter.

Keyed by API key when present, falling back to client IP address.
Sufficient for a single-process deployment. A shared store (Redis/Valkey)
will be required if the application is horizontally scaled.
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import net
from app.auth import is_valid_api_key
from app.config import settings

logger = logging.getLogger(__name__)

# Paths that are never rate-limited. /ws is deliberately absent: this is a
# BaseHTTPMiddleware, which never dispatches websocket scopes, so listing it
# here was dead code — the WS handshake limit lives in routers/websocket.py.
_EXEMPT_PATHS = {"/", "/health", "/health/ready", "/docs", "/redoc", "/openapi.json"}


class RateLimiter:
    """Sliding window counter, one deque per identity key."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._windows: dict[str, deque] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def check(self, key: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds).

        Evicts timestamps outside the current window before deciding.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds

        async with self._lock:
            window = self._windows[key]

            while window and window[0] <= cutoff:
                window.popleft()

            if len(window) >= self.max_requests:
                retry_after = int(self.window_seconds - (now - window[0])) + 1
                return False, retry_after

            window.append(now)
            return True, 0

    async def _run_cleanup(self):
        """Periodically evict windows where all timestamps have expired.

        Runs every window_seconds to bound memory growth from unique keys
        that stop making requests (their deques stay populated until the
        next check() call would evict them).
        """
        while True:
            await asyncio.sleep(self.window_seconds)
            cutoff = time.monotonic() - self.window_seconds
            async with self._lock:
                stale = [k for k, dq in self._windows.items() if not dq or dq[-1] <= cutoff]
                for k in stale:
                    del self._windows[k]
            if stale:
                logger.debug(f"Rate limiter: pruned {len(stale)} stale window(s)")

    def start_cleanup(self):
        """Schedule the background cleanup coroutine. Call from app lifespan."""
        self._cleanup_task = asyncio.create_task(self._run_cleanup())

    def stop_cleanup(self):
        """Cancel the background cleanup coroutine. Call from app lifespan shutdown."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply per-key rate limiting to all non-exempt routes."""

    def __init__(self, app, limiter: RateLimiter):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        # The rate-limit identity is the API key ONLY when it is a VALID
        # key. Bucketing on the raw header value gave every key-guessing
        # attempt its own fresh window, so brute-forcing the key was never
        # throttled; invalid/absent keys now share the client IP's bucket.
        # IP derivation (incl. trusted-proxy forwarded-header rules) lives
        # in app.net.client_ip; spoofable left-most XFF entries never win.
        supplied = request.headers.get(settings.API_KEY_HEADER)
        if supplied and is_valid_api_key(supplied):
            key = supplied
        else:
            key = f"ip:{net.client_ip(request) or 'unknown'}"

        allowed, retry_after = await self.limiter.check(key)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
