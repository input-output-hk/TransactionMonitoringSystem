"""SQL-shape guard for the baselines percentile query builder.

``_query_percentiles`` (optionally scoped) computes the p50/p99 anchors every
scorer normalises against and that feed drift detection. The scope predicate
MUST be applied INSIDE the feature subquery (before the chain-time JOIN), so it
cannot shrink the window side or change which feature rows feed the quantiles.
No test pinned this before the scoped/unscoped builders were consolidated.
"""

from __future__ import annotations

import pytest

from app.analysis import baselines
from app.db import clickhouse


class _RecordingClient:
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return [(1.0, 9.0, 5)]


@pytest.fixture
def rec(monkeypatch):
    client = _RecordingClient()
    monkeypatch.setattr(clickhouse, "_get_client", lambda: client)
    return client


def test_scoped_predicate_is_inside_feature_subquery(rec):
    out = baselines._query_percentiles_scoped(
        "utxo_features", "ada_amount", "preprod", "address", "addr1xyz", 90
    )
    assert out == (1.0, 9.0, 5)
    sql = rec.sql
    assert "address = %(scope_value)s" in sql
    # The scope predicate must sit in the feature subquery, BEFORE the JOIN, so
    # it filters which feature rows enter the quantile (not the window side).
    before_join = sql.split(" JOIN ", 1)[0]
    assert "address = %(scope_value)s" in before_join
    assert "utxo_features FINAL" in before_join
    assert rec.params["scope_value"] == "addr1xyz"


def test_unscoped_has_no_scope_predicate(rec):
    out = baselines._query_percentiles("utxo_features", "ada_amount", "preprod", 90)
    assert out == (1.0, 9.0, 5)
    assert "scope_value" not in rec.sql
    assert " JOIN " in rec.sql  # still the windowed chain-time join


def test_disallowed_inputs_rejected(rec):
    with pytest.raises(ValueError):
        baselines._query_percentiles("bad_table", "ada_amount", "preprod", 90)
    with pytest.raises(ValueError):
        baselines._query_percentiles_scoped(
            "utxo_features", "ada_amount", "preprod", "bad_col", "x", 90
        )
