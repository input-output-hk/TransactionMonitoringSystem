"""Fixtures shared by the opt-in live-database tiers (live_db and perf).

Both tiers talk to real servers through the same client bootstrap; keeping
one implementation here means a connection or schema-application fix lands
in every live tier at once instead of drifting between copies. Each tier's
conftest re-exports these and owns only its collection gate.
"""

import pytest


@pytest.fixture(autouse=True)
def mock_clickhouse_baseline():
    """Override the suite-wide autouse baseline mock (tests/conftest.py):
    live tiers must hit the real ClickHouse, that is their entire purpose."""
    yield


@pytest.fixture(scope="module")
def ch():
    """Live ClickHouse client with the real schema applied."""
    from app.db import clickhouse

    clickhouse.init_client()
    clickhouse.execute_schema()
    yield clickhouse
    clickhouse.close_client()
