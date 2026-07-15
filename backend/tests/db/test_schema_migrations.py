"""Schema migration gating for the transactions projection swap.

The p_by_time (SELECT *) -> p_by_time_v2 (scalar columns) swap involves a
MATERIALIZE PROJECTION, a part-rewriting mutation that must run once per
deployment, not on every boot. The gate is the live CREATE TABLE text.
"""

from datetime import UTC
from unittest.mock import MagicMock

from app.db.clickhouse_schema import migrate_transactions_projection


def _client_with_ddl(ddl: str) -> MagicMock:
    client = MagicMock()
    client.execute.side_effect = lambda q, *a, **k: [(ddl,)] if "system.tables" in q else None
    return client


class TestProjectionMigrationGate:
    def test_legacy_projection_triggers_swap(self):
        client = _client_with_ddl(
            "CREATE TABLE transactions (... PROJECTION p_by_time "
            "(SELECT * ORDER BY network, timestamp) ...)"
        )
        migrate_transactions_projection(client)
        statements = [c.args[0] for c in client.execute.call_args_list]
        alters = [s for s in statements if s.startswith("ALTER TABLE transactions")]
        # The dedup-projection setting must precede ADD PROJECTION: CH >= 24.7
        # rejects projections on a ReplacingMergeTree without it.
        assert "MODIFY SETTING deduplicate_merge_projection_mode = 'rebuild'" in alters[0]
        assert "DROP PROJECTION IF EXISTS p_by_time" in alters[1]
        assert "ADD PROJECTION IF NOT EXISTS p_by_time_v2" in alters[2]
        assert "MATERIALIZE PROJECTION p_by_time_v2" in alters[3]

    def test_v2_projection_is_noop(self):
        client = _client_with_ddl(
            "CREATE TABLE transactions (... PROJECTION p_by_time_v2 "
            "(SELECT tx_hash, network ORDER BY network, timestamp) ...)"
        )
        migrate_transactions_projection(client)
        statements = [c.args[0] for c in client.execute.call_args_list]
        assert not any(s.startswith("ALTER TABLE") for s in statements)

    def test_narrowed_projection_excludes_raw_data(self):
        # raw_data is the dominant column of the largest table; projecting it
        # doubled storage and merge IO for queries that never select it.
        from app.db.clickhouse_schema import _TX_PROJECTION_SELECT

        assert "raw_data" not in _TX_PROJECTION_SELECT
        assert "metadata" not in _TX_PROJECTION_SELECT
        assert "ingestion_timestamp" in _TX_PROJECTION_SELECT  # RMT version column


class TestCreateAllMigrationOrder:
    """The projection swap's ADD PROJECTION selects the network column, so
    create_all must run it AFTER the transactions column migrations: on a
    pre-network-column deployment the unguarded ALTER crashed startup."""

    def test_projection_migration_runs_after_column_migrations(self):
        from app.db.clickhouse_schema import create_all

        legacy_tx_ddl = (
            "CREATE TABLE transactions (... PROJECTION p_by_time "
            "(SELECT * ORDER BY network, timestamp) ...) "
            "ENGINE = ReplacingMergeTree ..."
        )
        client = MagicMock()

        def execute(q, *a, **k):
            if "create_table_query" in q:
                # Projection gate + TTL-removal check both read the live DDL;
                # the legacy text (no p_by_time_v2) forces the migration.
                return [(legacy_tx_ddl,)]
            if "engine_full" in q:
                return []  # assert_no_legacy_schema: nothing legacy
            if "system.columns" in q:
                return []  # baselines table absent
            return None

        client.execute.side_effect = execute
        create_all(client)

        statements = [c.args[0] for c in client.execute.call_args_list]
        idx_projection = next(
            i for i, s in enumerate(statements) if "ADD PROJECTION IF NOT EXISTS p_by_time_v2" in s
        )
        for needle in (
            "ADD COLUMN IF NOT EXISTS network",
            "ADD COLUMN IF NOT EXISTS block_index",
            "MODIFY COLUMN total_input_value",
            "ADD COLUMN IF NOT EXISTS script_valid",
        ):
            idx_col = next(
                i
                for i, s in enumerate(statements)
                if s.startswith("ALTER TABLE transactions ") and needle in s
            )
            assert idx_col < idx_projection, f"'{needle}' must run before the projection migration"


class TestRetentionTtlRemoval:
    """A knob back at 0 must REMOVE any stale TTL clause; otherwise
    "0 = keep forever" is false after one enable/disable cycle and the old
    TTL keeps deleting rows forever (review finding)."""

    def _client(self, ddl: str) -> MagicMock:
        client = MagicMock()
        client.execute.side_effect = lambda q, *a, **k: [(ddl,)] if "system.tables" in q else None
        return client

    def test_stale_ttl_removed_when_knob_zero(self, monkeypatch):
        from app.config import settings
        from app.db.clickhouse_schema import apply_retention_ttls

        for knob in (
            "CH_RETENTION_DAYS_TRANSACTIONS",
            "CH_RETENTION_DAYS_IO",
            "CH_RETENTION_DAYS_FEATURES",
        ):
            monkeypatch.setattr(settings, knob, 0)
        client = self._client(
            "CREATE TABLE transactions (...) ENGINE = ReplacingMergeTree "
            "TTL ingestion_timestamp + INTERVAL 30 DAY ..."
        )
        apply_retention_ttls(client)
        removes = [c.args[0] for c in client.execute.call_args_list if "REMOVE TTL" in c.args[0]]
        assert removes  # every zeroed knob with a live TTL clause clears it

    def test_no_remove_when_table_never_had_ttl(self, monkeypatch):
        from app.config import settings
        from app.db.clickhouse_schema import apply_retention_ttls

        for knob in (
            "CH_RETENTION_DAYS_TRANSACTIONS",
            "CH_RETENTION_DAYS_IO",
            "CH_RETENTION_DAYS_FEATURES",
        ):
            monkeypatch.setattr(settings, knob, 0)
        client = self._client("CREATE TABLE transactions (...) ENGINE = ReplacingMergeTree ...")
        apply_retention_ttls(client)
        assert not any("REMOVE TTL" in c.args[0] for c in client.execute.call_args_list)

    def test_remove_ttl_failure_does_not_block_startup(self, monkeypatch):
        from clickhouse_driver.errors import Error as ClickHouseError

        from app.db.clickhouse_schema import _remove_table_ttl

        client = MagicMock()

        def execute(q, *a, **k):
            if "system.tables" in q:
                return [("CREATE TABLE x ... TTL ingestion_timestamp ...",)]
            raise ClickHouseError("concurrent removal")

        client.execute.side_effect = execute
        _remove_table_ttl(client, "transactions")  # must not raise


class TestInsertShape:
    """The writer must persist the new failed-tx columns."""

    def test_insert_includes_script_valid_and_attempt_flags(self, monkeypatch):
        from datetime import datetime

        from app.db import clickhouse
        from app.models.transaction import (
            NormalizedTransaction,
            TransactionInput,
            TransactionOutput,
        )

        client = MagicMock()
        monkeypatch.setattr(clickhouse, "_get_client", lambda: client)
        tx = NormalizedTransaction(
            tx_hash="ff" * 32,
            network="preprod",
            timestamp=datetime.now(UTC),
            fee=0,
            script_valid=False,
            inputs=[
                TransactionInput(
                    tx_hash="aa" * 32,
                    index=0,
                    address="",
                    amount=0,
                    is_unspent_attempt=True,
                ),
            ],
            outputs=[
                TransactionOutput(
                    address="addr_test1x",
                    amount=5,
                    is_collateral=True,
                    output_index=3,
                ),
            ],
            raw_data={},
        )
        clickhouse.insert_transactions_batch([tx])
        statements = {
            c.args[0]: c.args[1] for c in client.execute.call_args_list if len(c.args) > 1
        }
        tx_insert = next(q for q in statements if "INSERT INTO transactions" in q)
        assert "script_valid" in tx_insert
        in_insert = next(q for q in statements if "INSERT INTO transaction_inputs" in q)
        assert "is_unspent_attempt" in in_insert
        assert statements[in_insert][0][10] == 1  # is_unspent_attempt flag set
        out_insert = next(q for q in statements if "INSERT INTO transaction_outputs" in q)
        # Explicit on-chain index wins over the enumerate position.
        assert statements[out_insert][0][2] == 3


class TestWideCountColumnGuard:
    """UInt8 count/index columns overflow on 256+-input transactions — the
    insert fails and chain sync wedges at that block forever (observed live
    on preprod). The columns sit in ORDER BY keys and the transactions
    projection, so they cannot be ALTERed in place: the startup guard must
    force the rebuild migration instead of letting the app run and wedge."""

    @staticmethod
    def _client(types_by_table):
        from app.db.clickhouse_schema import DEDUP_TABLE_KEYS

        client = MagicMock()

        def execute(q, params=None, *a, **k):
            if "engine_full" in q:
                # Every dedup table reports a clean v2 engine: the narrow
                # columns must trip the guard on their own.
                return [
                    (t, "ReplacingMergeTree", "ReplacingMergeTree ORDER BY ...")
                    for t in DEDUP_TABLE_KEYS
                ]
            if "system.columns" in q:
                table = params["t"]
                return list(types_by_table.get(table, {}).items())
            return None

        client.execute.side_effect = execute
        return client

    def test_narrow_input_count_refuses_startup(self):
        import pytest

        from app.db.clickhouse_schema import assert_no_legacy_schema

        client = self._client(
            {
                "transactions": {"input_count": "UInt8", "output_count": "UInt16"},
            }
        )
        with pytest.raises(RuntimeError, match="transactions.input_count"):
            assert_no_legacy_schema(client)

    def test_wide_columns_pass(self):
        from app.db.clickhouse_schema import (
            WIDE_COUNT_COLUMNS,
            assert_no_legacy_schema,
        )

        client = self._client(dict(WIDE_COUNT_COLUMNS))
        assert_no_legacy_schema(client)  # must not raise

    def test_missing_table_is_not_stale(self):
        # Tables that don't exist yet are created fresh from SCHEMA_DDL;
        # the guard must not demand a migration for them.
        from app.db.clickhouse_schema import stale_count_columns

        client = self._client({})
        assert stale_count_columns(client, "transaction_inputs") == []

    def test_ddl_and_enforcement_map_agree(self):
        # The DDL is the single source of truth; the enforcement map must
        # never demand a type the CREATE TABLE doesn't produce.
        from app.db.clickhouse_schema import SCHEMA_DDL, WIDE_COUNT_COLUMNS

        for table, cols in WIDE_COUNT_COLUMNS.items():
            ddl = SCHEMA_DDL[table]
            for col, want in cols.items():
                assert f"{col} {want}" in ddl, f"{table}.{col}: DDL does not declare {want}"
