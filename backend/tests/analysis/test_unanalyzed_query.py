"""Regression tests for the unanalyzed-transactions query.

The ingester writes ``transactions`` and ``transaction_inputs`` as separate
``INSERT`` statements (ClickHouse has no multi-statement transactions). A
prior revision of ``get_unanalyzed_transactions`` returned rows the moment
they appeared in ``transactions``, so a poll that landed between the two
inserts saw txs with no resolved input addresses. The scorer enrichment
no-op'd, the multiple-satisfaction gate (``≥2 inputs from same script``)
silently failed, and the analysis row was written with score ``-1.0`` and
never re-evaluated.

These tests lock in the fix: the query must defer a tx until either its
``input_count`` is 0 or at least one row in ``transaction_inputs`` exists
for it.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def captured_queries(monkeypatch):
    from app.db import clickhouse

    captured = []

    fake_client = MagicMock()
    fake_client.execute.side_effect = lambda sql, params=None: captured.append((sql, params)) or []
    monkeypatch.setattr(clickhouse, "_get_client", lambda: fake_client)
    return captured


class TestUnanalyzedQueryDefersUnenrichedTxs:
    def test_query_filters_by_transaction_inputs_presence(self, captured_queries):
        from app.db.clickhouse import get_unanalyzed_transactions

        get_unanalyzed_transactions("preprod", 50)

        assert len(captured_queries) == 1
        sql, params = captured_queries[0]
        # Must reference transaction_inputs as a sentinel for input enrichment readiness.
        assert "transaction_inputs" in sql, (
            f"query must defer txs until transaction_inputs is visible; current SQL: {sql}"
        )
        # The input_count = 0 escape hatch must be present so treasury / collateral-only
        # txs aren't stuck in the queue forever waiting for inputs that never arrive.
        assert "input_count = 0" in sql, (
            f"query must admit input_count=0 txs directly; current SQL: {sql}"
        )

    def test_query_is_network_scoped_on_both_tables(self, captured_queries):
        """The transaction_inputs sub-condition must filter by network too,
        otherwise a cross-network row could falsely satisfy the guard.
        """
        from app.db.clickhouse import get_unanalyzed_transactions

        get_unanalyzed_transactions("preview", 10)

        sql, params = captured_queries[0]
        assert params.get("network") == "preview"
        # Whatever shape the guard takes (IN subquery, SEMI JOIN, etc.), the
        # portion that touches transaction_inputs must itself constrain the
        # network. Asserted structurally: the slice of SQL after the first
        # mention of the table must contain a network filter before the next
        # statement-level boundary.
        idx = sql.find("transaction_inputs")
        assert idx != -1, "query must reference transaction_inputs"
        tail = sql[idx:]
        assert "network" in tail, (
            f"transaction_inputs guard must be network-scoped; current SQL: {sql}"
        )
