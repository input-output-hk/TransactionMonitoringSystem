"""Regression tests for the unanalyzed-transactions query's readiness rule.

The ingester writes a block's tables as separate ``INSERT`` statements
(ClickHouse has no multi-statement transactions). A prior revision returned
rows the moment they appeared in ``transactions`` while ``transactions`` was
written first, so a poll that landed between the inserts saw txs with no
resolved input addresses: the multiple-satisfaction gate (``≥2 inputs from
same script``) silently failed and the row was stored at ``-1.0`` for good.

The writer now commits ``transactions`` last (tests/db/test_block_write_order),
so a visible row is a ready row and the query needs no witness. The witness it
used to take, a ``transaction_inputs`` subquery, read that table on every pick
and passed over for good a transaction whose inputs had expired by retention
before it was scored. These tests pin that it stays gone.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

SINCE = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def captured_queries(monkeypatch):
    from app.db import clickhouse

    captured = []

    fake_client = MagicMock()
    fake_client.execute.side_effect = lambda sql, params=None: captured.append((sql, params)) or []
    monkeypatch.setattr(clickhouse, "_get_client", lambda: fake_client)
    return captured


class TestUnanalyzedQueryReadiness:
    def test_a_visible_transaction_needs_no_inputs_witness(self, captured_queries):
        from app.db.clickhouse import get_unanalyzed_transactions

        get_unanalyzed_transactions("preprod", 50, since=SINCE)

        assert len(captured_queries) == 1
        sql, _ = captured_queries[0]
        assert "transaction_inputs" not in sql, (
            f"an inputs witness would pass over txs whose inputs expired; current SQL: {sql}"
        )

    def test_query_is_network_scoped(self, captured_queries):
        from app.db.clickhouse import get_unanalyzed_transactions

        get_unanalyzed_transactions("preview", 10, since=SINCE)

        sql, params = captured_queries[0]
        assert params.get("network") == "preview"
        # The outer read and both scores-side reads.
        assert sql.count("network = %(network)s") == 3
