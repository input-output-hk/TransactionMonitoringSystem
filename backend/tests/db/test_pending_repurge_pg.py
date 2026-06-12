"""Durable queue for the delayed tx_class_scores rollback repurge.

The in-memory repurge task is volatile (restart, shutdown inside the delay
window, GC), and a lost repurge leaves a stale score row permanently
blocking re-scoring of a re-confirmed tx (missed-attack risk). These tests
pin the Postgres persistence contract: idempotent schema creation, write
before scheduling (ON CONFLICT-safe), and targeted clearing.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db import postgres


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def conn(monkeypatch):
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock(return_value="DELETE 0")
    mock_conn.executemany = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value=1)

    @asynccontextmanager
    async def fake_get_connection():
        yield mock_conn

    monkeypatch.setattr(postgres, "get_connection", fake_get_connection)
    return mock_conn


class TestSchema:
    def test_schema_creates_pending_score_repurges(self, conn):
        _run(postgres.execute_schema())
        ddl = next(
            c.args[0] for c in conn.execute.call_args_list
            if "pending_score_repurges" in c.args[0]
        )
        # Idempotent (IF NOT EXISTS) with the composite natural key: one
        # row per (network, tx_hash) regardless of rollback re-delivery.
        assert "CREATE TABLE IF NOT EXISTS pending_score_repurges" in ddl
        assert "PRIMARY KEY (network, tx_hash)" in ddl


class TestAddPending:
    def test_writes_one_row_per_hash_conflict_safe(self, conn):
        hashes = ["aa" * 32, "bb" * 32]
        _run(postgres.add_pending_score_repurges("preprod", hashes))
        sql, rows = conn.executemany.await_args.args
        # The node re-sends rollbacks (the cleanup is idempotent), so the
        # insert must tolerate hashes that are already queued.
        assert "ON CONFLICT (network, tx_hash) DO NOTHING" in sql
        assert rows == [("preprod", hashes[0]), ("preprod", hashes[1])]

    def test_empty_list_skips_db(self, conn):
        _run(postgres.add_pending_score_repurges("preprod", []))
        conn.executemany.assert_not_awaited()


class TestGetPending:
    def test_returns_hashes_for_network(self, conn):
        conn.fetch.return_value = [
            {"tx_hash": "aa" * 32}, {"tx_hash": "bb" * 32},
        ]
        result = _run(postgres.get_pending_score_repurges("preprod"))
        assert result == ["aa" * 32, "bb" * 32]
        assert conn.fetch.await_args.args[-1] == "preprod"


class TestClearPending:
    def test_deletes_only_given_hashes(self, conn):
        hashes = ["aa" * 32]
        _run(postgres.clear_pending_score_repurges("preprod", hashes))
        sql, network, cleared = conn.execute.await_args.args
        assert "DELETE FROM pending_score_repurges" in sql
        assert "ANY($2)" in sql  # targeted: never wipes another rollback's queue
        assert network == "preprod"
        assert cleared == hashes

    def test_empty_list_skips_db(self, conn):
        _run(postgres.clear_pending_score_repurges("preprod", []))
        conn.execute.assert_not_awaited()
