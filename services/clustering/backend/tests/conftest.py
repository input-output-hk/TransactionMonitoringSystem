"""Test-wide fixtures.

The suite is hermetic: no network, no ClickHouse, and — enforced here — no
leakage from the host/container environment. ``get_settings()`` is cached and
read by the API auth dependency and the ingest/service defaults, so a deployed
container's ``API_KEY`` (or tuning vars) would otherwise change test behaviour.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.config import get_settings

# Env vars that alter code paths under test; cleared so the suite behaves the
# same on a laptop, in CI, and inside a hardened deployed container.
_SETTINGS_ENV = (
    "API_KEY",
    "MODEL_SIGNING_KEYS",
    "CORS_ORIGINS",
    "BLOCKFROST_PROJECT_ID",
    "CHAIN_SOURCE",
    "MAX_INFLIGHT_JOBS",
    "INGEST_BATCH_SIZE",
    "INGEST_CONCURRENCY",
    "MAX_GRAPH_TXS",
    "LOG_LEVEL",
    "LOG_FORMAT",
)


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in _SETTINGS_ENV:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
