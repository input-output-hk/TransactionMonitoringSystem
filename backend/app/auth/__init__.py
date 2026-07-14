"""Authentication package.

Two coexisting mechanisms:

- **API keys** (`api_key.py`) — server-to-server, header-based, opaque keys
  configured via ``API_KEYS`` env var. Used by external integrations.

- **Magic-link sessions** (`tokens.py`, `sessions.py`, `email.py`, `deps.py`)
  — human users, cookie-based opaque session IDs backed by Postgres rows.
  Magic-link tokens are one-time and short-lived (15 min default).

The legacy module path ``app.auth`` re-exports the API-key helpers so
existing imports (``from app.auth import verify_api_key``) keep working
unchanged while new auth flows live in submodules.
"""

from app.auth.api_key import (
    _dev_mode,
    api_key_header,
    is_valid_api_key,
    verify_api_key,
)

__all__ = [
    "_dev_mode",
    "api_key_header",
    "is_valid_api_key",
    "verify_api_key",
]
