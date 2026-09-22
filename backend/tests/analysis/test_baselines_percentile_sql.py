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
from app.config import settings
from app.db import clickhouse


class _RecordingClient:
    # Mirrors clickhouse_driver's Client.execute signature for the arguments the
    # baseline queries pass, so an unexpected keyword fails here instead of being
    # swallowed by _query_percentiles's own error handling.
    def __init__(self):
        self.sql = None
        self.params = None
        self.settings = None

    def execute(self, sql, params=None, settings=None):
        self.sql = sql
        self.params = params
        self.settings = settings
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


def test_percentile_scans_carry_the_thread_cap(rec):
    """Every percentile scan runs thread-capped.

    Uncapped, the ~1,850 scans of one recompute held ~6.5 cores of the mainnet
    host for ~17 minutes. The cap is read from settings at call time, so the
    assertion follows the configured value rather than restating it.
    """
    baselines._query_percentiles("utxo_features", "ada_amount", "preprod", 90)
    assert rec.settings == {"max_threads": settings.BASELINE_QUERY_MAX_THREADS}
    baselines._query_percentiles_scoped(
        "utxo_features", "ada_amount", "preprod", "address", "addr1xyz", 90
    )
    assert rec.settings == {"max_threads": settings.BASELINE_QUERY_MAX_THREADS}


def test_thread_cap_follows_the_live_setting(rec, monkeypatch):
    """A retuned cap applies without a restart of the module."""
    monkeypatch.setattr(
        settings, "BASELINE_QUERY_MAX_THREADS", settings.BASELINE_QUERY_MAX_THREADS + 1
    )
    baselines._query_percentiles("utxo_features", "ada_amount", "preprod", 90)
    assert rec.settings == {"max_threads": settings.BASELINE_QUERY_MAX_THREADS}
