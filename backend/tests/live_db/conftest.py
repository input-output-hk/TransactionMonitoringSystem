"""Opt-in live-database test tier.

The hermetic suite mocks every ClickHouse/Postgres call at the function
boundary, which is how two real ClickHouse 26.x regressions shipped with
green tests (aggregate-alias shadowing, projection-gate DDL). This tier
applies the real schema and runs representative queries against live
servers, so version- and dialect-level breakage surfaces in CI instead of
production.

Opt in with TMS_LIVE_DB_TESTS=1; without it the whole directory is
skipped at collection and `pytest tests/` stays hermetic. Connection
settings come from the normal app environment (POSTGRES_HOST/PORT/...,
CLICKHOUSE_HOST/PORT/...). With the repo docker-compose defaults that
means POSTGRES_PORT=5433 locally. CI runs this tier in the `live-db` job
against service containers pinned to the docker-compose image versions.

Everything written by these tests lives under the LIVE_NETWORK namespace
or uses throwaway UUID-based identities, so pointing them at a dev
database does not pollute operator-visible data.
"""

import asyncio
import os

import pytest

_LIVE_DB_ENV = "TMS_LIVE_DB_TESTS"

# Namespace for rows these tests write: every read path is network-scoped,
# so synthetic rows in this network are invisible to real dashboards.
LIVE_NETWORK = "livedbtest"

if not os.environ.get(_LIVE_DB_ENV):
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture(autouse=True)
def mock_clickhouse_baseline():
    """Override the suite-wide autouse baseline mock: this tier must hit
    the real ClickHouse, that is its entire purpose."""
    yield


@pytest.fixture
def pg_run():
    """Run an async scenario with a live Postgres pool and schema.

    asyncpg pools bind to the event loop that created them, so the pool
    lifecycle must live inside the same asyncio.run as the scenario;
    a plain async fixture on a per-test loop would hand tests a dead pool.
    """
    from app.db import postgres

    def runner(scenario):
        async def _wrapped():
            await postgres.init_pool()
            try:
                await postgres.execute_schema()
                from app.auth.schema import execute_auth_schema

                await execute_auth_schema()
                return await scenario()
            finally:
                await postgres.close_pool()

        return asyncio.run(_wrapped())

    return runner


@pytest.fixture(scope="module")
def ch():
    """Live ClickHouse client with the real schema applied."""
    from app.db import clickhouse

    clickhouse.init_client()
    clickhouse.execute_schema()
    yield clickhouse
    clickhouse.close_client()
