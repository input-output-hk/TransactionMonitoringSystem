"""Dedup-migration chunking and crash recovery.

The collapse INSERT...SELECT...GROUP BY holds argMax(raw_data, ...) state
per distinct key; unchunked over a production table it exhausts server
memory (review finding). Buckets hash tx_hash (present in every dedup key,
so a group never splits) and each INSERT carries explicit memory settings.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "migrate_dedup_schema.py",
)


@pytest.fixture
def mig():
    spec = importlib.util.spec_from_file_location("migrate_dedup_schema", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_client(mig_table_exists=False):
    """Client mimicking a legacy MergeTree table pending migration."""
    client = MagicMock()
    state = {"count_calls": 0}

    def execute(q, *a, **k):
        if "FROM system.tables" in q:
            name = a[0]["t"] if a else None
            if name and name.endswith("__mig"):
                return [("MergeTree", "")] if mig_table_exists else []
            return [("MergeTree", "MergeTree ORDER BY tx_hash")]
        if "name, type" in q:
            # stale_count_columns probe: columns already at the DDL width,
            # so the legacy ENGINE alone drives these migrations.
            return []
        if "FROM system.columns" in q:
            return [("tx_hash",), ("network",), ("ingestion_timestamp",)]
        if q.startswith("SELECT count() FROM (SELECT"):
            return [(100,)]
        if q.startswith("SELECT count() FROM"):
            # First call: legacy total; second: migrated count (must match
            # the distinct-keys count for the verification to pass).
            state["count_calls"] += 1
            return [(100,)]
        return None

    client.execute.side_effect = execute
    return client


class TestChunkedCollapse:
    def test_collapse_runs_one_insert_per_bucket_with_memory_settings(self, mig):
        client = _legacy_client()
        mig.migrate_table(
            client, "transactions", apply=True, legacy_suffix="20260611",
            buckets=4, max_memory_bytes=1_000_000,
        )
        inserts = [
            c for c in client.execute.call_args_list
            if c.args[0].startswith("INSERT INTO transactions__mig")
        ]
        assert len(inserts) == 4
        for i, call in enumerate(inserts):
            assert "cityHash64(tx_hash)" in call.args[0]
            assert call.args[1] == {"buckets": 4, "bucket": i}
            settings = call.kwargs["settings"]
            assert settings["max_memory_usage"] == 1_000_000
            # Spill threshold at half the budget keeps merge-phase headroom.
            assert settings["max_bytes_before_external_group_by"] == 500_000

    def test_collapse_select_never_aliases_aggregates(self, mig):
        # Regression: aliasing max(ingestion_timestamp) AS ingestion_timestamp
        # made the ClickHouse analyzer substitute the alias into the sibling
        # argMax(col, ingestion_timestamp) expressions and reject the query
        # as a nested aggregate (Code 184; reproduced live on 26.1.3). The
        # INSERT's explicit column list maps positionally, so the SELECT
        # aggregates must carry no AS aliases at all.
        client = _legacy_client()
        mig.migrate_table(
            client, "transactions", apply=True, legacy_suffix="20260612",
            buckets=1, max_memory_bytes=1_000_000,
        )
        inserts = [
            c.args[0] for c in client.execute.call_args_list
            if c.args[0].startswith("INSERT INTO transactions__mig")
        ]
        assert inserts
        for sql in inserts:
            select_part = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
            assert " AS " not in select_part, sql
            assert "max(ingestion_timestamp)" in select_part

    def test_count_mismatch_still_aborts(self, mig):
        client = _legacy_client()
        original = client.execute.side_effect

        def execute(q, *a, **k):
            if q == "SELECT count() FROM transactions__mig":
                return [(99,)]  # one row short -> abort
            return original(q, *a, **k)

        client.execute.side_effect = execute
        with pytest.raises(RuntimeError, match="migrated row count"):
            mig.migrate_table(
                client, "transactions", apply=True, legacy_suffix="20260611",
            )
        # Live table untouched: no EXCHANGE issued.
        assert not any(
            c.args[0].startswith("EXCHANGE") for c in client.execute.call_args_list
        )


class TestStrandedMigRecovery:
    def test_stranded_mig_renamed_when_live_already_v2(self, mig):
        # Crash between EXCHANGE and RENAME leaves post-swap legacy data
        # under __mig while the live table is already v2; re-runs must
        # recover it instead of skipping forever.
        client = MagicMock()

        def execute(q, *a, **k):
            if "FROM system.tables" in q:
                name = a[0]["t"]
                if name.endswith("__mig"):
                    return [("MergeTree", "MergeTree ORDER BY tx_hash")]
                return [("ReplacingMergeTree", "ReplacingMergeTree(ingestion_timestamp) ORDER BY tx_hash")]
            if "name, type" in q:
                return []  # columns already wide
            return None

        client.execute.side_effect = execute
        swapped = mig.migrate_table(
            client, "transactions", apply=True, legacy_suffix="20260611",
        )
        assert swapped is False
        renames = [
            c.args[0] for c in client.execute.call_args_list
            if c.args[0].startswith("RENAME TABLE transactions__mig")
        ]
        assert renames == [
            "RENAME TABLE transactions__mig TO transactions__legacy_20260611"
        ]

    def test_v2_without_stranded_mig_skips_quietly(self, mig):
        client = MagicMock()

        def execute(q, *a, **k):
            if "FROM system.tables" in q:
                name = a[0]["t"]
                if name.endswith("__mig"):
                    return []
                return [("ReplacingMergeTree", "ReplacingMergeTree(x) ORDER BY tx_hash")]
            if "name, type" in q:
                return []  # columns already wide
            return None

        client.execute.side_effect = execute
        assert mig.migrate_table(
            client, "transactions", apply=True, legacy_suffix="20260611",
        ) is False
        assert not any(
            c.args[0].startswith("RENAME") for c in client.execute.call_args_list
        )


class TestNarrowColumnRebuild:
    """A v2 engine with UInt8-era count columns must still be rebuilt: the
    columns overflow on 256+-input transactions and sit in ORDER BY keys /
    the transactions projection, so ALTER MODIFY cannot widen them."""

    @staticmethod
    def _v2_narrow_client():
        client = MagicMock()
        state = {"count_calls": 0}

        def execute(q, *a, **k):
            if "FROM system.tables" in q:
                name = a[0]["t"] if a else None
                if name and name.endswith("__mig"):
                    return []
                return [(
                    "ReplacingMergeTree",
                    "ReplacingMergeTree(ingestion_timestamp) ORDER BY (network, tx_hash)",
                )]
            if "name, type" in q:
                return [("input_count", "UInt8"), ("output_count", "UInt16")]
            if "FROM system.columns" in q:
                return [("tx_hash",), ("network",), ("ingestion_timestamp",)]
            if q.startswith("SELECT count() FROM (SELECT"):
                return [(100,)]
            if q.startswith("SELECT count() FROM"):
                state["count_calls"] += 1
                return [(100,)]
            return None

        client.execute.side_effect = execute
        return client

    def test_v2_engine_with_narrow_columns_is_rebuilt(self, mig):
        client = self._v2_narrow_client()
        swapped = mig.migrate_table(
            client, "transactions", apply=True, legacy_suffix="20260612",
        )
        assert swapped is True
        assert any(
            c.args[0] == "EXCHANGE TABLES transactions AND transactions__mig"
            for c in client.execute.call_args_list
        )
