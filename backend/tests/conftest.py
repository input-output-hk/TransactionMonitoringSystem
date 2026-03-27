"""Shared fixtures for the test suite.

Mocks the ClickHouse baseline lookup to avoid DB dependency in unit tests.
Scorers will see "missing" baselines and fall through to bootstrap defaults.
"""

import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def mock_clickhouse_baseline():
    """Mock get_baseline at the DB layer so no ClickHouse connection is needed."""
    with patch("app.db.clickhouse.get_baseline", return_value=None):
        yield
