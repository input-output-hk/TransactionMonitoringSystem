"""API Key Authentication for FastAPI"""

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


async def verify_api_key(api_key: Optional[str] = Security(api_key_header)) -> str:
    """Verify the API key and return it if valid."""
    if _dev_mode:
        return "dev-mode"

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key required. Provide it in the TMS-API-Key header.",
        )

    if api_key not in _valid_keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )

    return api_key
