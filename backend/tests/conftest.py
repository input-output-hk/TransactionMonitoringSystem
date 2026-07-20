"""Shared fixtures for the test suite.

Mocks the ClickHouse baseline lookup to avoid DB dependency in unit tests.
Scorers will see "missing" baselines and fall through to bootstrap defaults.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def mock_clickhouse_baseline():
    """Mock the per-tx ClickHouse lookups so no connection is needed.

    ``get_baseline`` -> None: scorers fall through to bootstrap anchors.
    ``get_policies_first_seen`` -> {}: token_dust sees "age unknown" and
    exercises the fail-open no-cap path, so every pre-existing test
    (including the CTF-06 attack pins) runs unchanged.
    """
    with (
        patch("app.db.clickhouse.get_baseline", return_value=None),
        patch("app.db.clickhouse.get_policies_first_seen", return_value={}),
    ):
        yield
