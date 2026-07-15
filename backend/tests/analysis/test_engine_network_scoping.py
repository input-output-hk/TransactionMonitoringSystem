"""Regression tests for network-scoped enrichment in the analysis engine.

A prior revision's `_enrich_inputs_with_resolved_addresses` issued two
ClickHouse queries against `transaction_inputs` and `transactions` without
filtering by network. When preprod and preview instances shared a
ClickHouse, enrichment for one network could pull rows from the other.

These tests lock the fix in by intercepting ClickHouse calls and asserting
every query carries `network = %(network)s` with the caller's network bound.
"""

from unittest.mock import MagicMock

import pytest


def _make_rows(network: str):
    """Minimal row shape expected by _enrich_inputs_with_resolved_addresses."""
    return [
        {
            "tx_hash": "a" * 64,
            "network": network,
            "raw_data": {
                "inputs": [
                    {
                        "transaction": {"id": "b" * 64},
                        "index": 0,
                    }
                ],
                "outputs": [],
            },
        }
    ]


@pytest.fixture
def captured_queries(monkeypatch):
    """Patch clickhouse._get_client to capture every execute() call."""
    from app.db import clickhouse

    captured = []

    fake_client = MagicMock()
    fake_client.execute.side_effect = lambda sql, params=None: captured.append((sql, params)) or []
    monkeypatch.setattr(clickhouse, "_get_client", lambda: fake_client)
    return captured


class TestEnrichmentQueriesAreNetworkScoped:
    def test_transaction_inputs_query_includes_network(
        self,
        captured_queries,
    ):
        """First enrichment read (transaction_inputs) must filter by network."""
        from app.analysis.engine import _enrich_inputs_with_resolved_addresses

        _enrich_inputs_with_resolved_addresses(_make_rows("preview"), "preview")

        assert captured_queries, "expected at least one ClickHouse query"
        sql, params = captured_queries[0]
        assert "transaction_inputs" in sql
        assert "network = %(network)s" in sql
        assert params["network"] == "preview"

    def test_transactions_fallback_query_includes_network(
        self,
        captured_queries,
    ):
        """Second enrichment read (transactions, for ref UTxO outputs)
        must filter by network."""
        from app.analysis.engine import _enrich_inputs_with_resolved_addresses

        _enrich_inputs_with_resolved_addresses(_make_rows("preprod"), "preprod")

        # Expect at least two queries: the input-address lookup, then the
        # referenced-tx-output lookup (only if there are ref tx hashes).
        fallback = [
            (sql, params) for (sql, params) in captured_queries if "FROM transactions" in sql
        ]
        assert fallback, "expected the ref-outputs query to fire"
        sql, params = fallback[0]
        assert "network = %(network)s" in sql
        assert params["network"] == "preprod"

    def test_network_is_never_cross_bound(self, captured_queries):
        """Every query issued during enrichment must bind the caller's network."""
        from app.analysis.engine import _enrich_inputs_with_resolved_addresses

        _enrich_inputs_with_resolved_addresses(_make_rows("mainnet"), "mainnet")

        for sql, params in captured_queries:
            assert params is not None, f"query without params: {sql!r}"
            assert params.get("network") == "mainnet", (
                f"unexpected network binding in query: {params!r}\nSQL: {sql}"
            )
