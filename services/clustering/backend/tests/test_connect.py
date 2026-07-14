"""Connection-parameter tests for the ClickHouse client factory.

``connect()`` is the single source of the driver kwargs; these tests pin that
both timeout knobs actually reach clickhouse_connect (an unpassed kwarg would
silently fall back to the driver default and the .env knob would do nothing).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.storage.clickhouse import base


def test_connect_passes_both_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get_client(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "client"

    monkeypatch.setattr(base.clickhouse_connect, "get_client", fake_get_client)
    settings = Settings(
        CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS=123.0,
        CLICKHOUSE_CONNECT_TIMEOUT_SECONDS=7.5,
    )

    assert base.connect(settings) == "client"
    assert captured["send_receive_timeout"] == 123.0
    assert captured["connect_timeout"] == 7.5


def test_connect_timeout_defaults_mirror_the_driver_split() -> None:
    """Defaults: 300s send/receive (HTTP returns nothing until the query ends,
    so long fits need the room; the host's native driver runs 120s) and the
    host-matching 10s connect ceiling."""
    settings = Settings()
    assert settings.clickhouse_send_receive_timeout_seconds == 300.0
    assert settings.clickhouse_connect_timeout_seconds == 10.0
