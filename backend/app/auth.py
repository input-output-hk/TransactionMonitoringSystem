"""API Key Authentication for FastAPI"""

import hmac
import logging
from typing import Optional, List
from fastapi import Security, HTTPException, status
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
    [k.strip() for k in settings.API_KEYS.split(",") if k.strip()]
    if settings.API_KEYS
    else []
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


async def verify_api_key(api_key: Optional[str] = Security(api_key_header)) -> str:
    """Verify the API key and return it if valid."""
    if _dev_mode:
        return "dev-mode"

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key required. Provide it in the TMS-API-Key header.",
        )

    if not is_valid_api_key(api_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )

    return api_key
