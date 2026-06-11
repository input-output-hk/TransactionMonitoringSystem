"""Schema migration gating for the transactions projection swap.

The p_by_time (SELECT *) -> p_by_time_v2 (scalar columns) swap involves a
MATERIALIZE PROJECTION, a part-rewriting mutation that must run once per
deployment, not on every boot. The gate is the live CREATE TABLE text.
"""

from unittest.mock import MagicMock

from app.db.clickhouse_schema import migrate_transactions_projection


def _client_with_ddl(ddl: str) -> MagicMock:
    client = MagicMock()
    client.execute.side_effect = lambda q, *a, **k: (
        [(ddl,)] if "system.tables" in q else None
    )
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


class TestRetentionTtlRemoval:
    """A knob back at 0 must REMOVE any stale TTL clause; otherwise
    "0 = keep forever" is false after one enable/disable cycle and the old
    TTL keeps deleting rows forever (review finding)."""

    def _client(self, ddl: str) -> MagicMock:
        client = MagicMock()
        client.execute.side_effect = lambda q, *a, **k: (
            [(ddl,)] if "system.tables" in q else None
        )
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
        removes = [
            c.args[0] for c in client.execute.call_args_list
            if "REMOVE TTL" in c.args[0]
        ]
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
        assert not any(
            "REMOVE TTL" in c.args[0] for c in client.execute.call_args_list
        )

    def test_remove_ttl_failure_does_not_block_startup(self, monkeypatch):
        from clickhouse_driver.errors import Error as ClickHouseError

        from app.config import settings
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
        from datetime import datetime, timezone

        from app.db import clickhouse
        from app.models.transaction import (
            NormalizedTransaction, TransactionInput, TransactionOutput,
        )

        client = MagicMock()
        monkeypatch.setattr(clickhouse, "_get_client", lambda: client)
        tx = NormalizedTransaction(
            tx_hash="ff" * 32,
            network="preprod",
            timestamp=datetime.now(timezone.utc),
            fee=0,
            script_valid=False,
            inputs=[
                TransactionInput(
                    tx_hash="aa" * 32, index=0, address="", amount=0,
                    is_unspent_attempt=True,
                ),
            ],
            outputs=[
                TransactionOutput(
                    address="addr_test1x", amount=5, is_collateral=True,
                    output_index=3,
                ),
            ],
            raw_data={},
        )
        clickhouse.insert_transactions_batch([tx])
        statements = {c.args[0]: c.args[1] for c in client.execute.call_args_list if len(c.args) > 1}
        tx_insert = next(q for q in statements if "INSERT INTO transactions" in q)
        assert "script_valid" in tx_insert
        in_insert = next(q for q in statements if "INSERT INTO transaction_inputs" in q)
        assert "is_unspent_attempt" in in_insert
        assert statements[in_insert][0][10] == 1  # is_unspent_attempt flag set
        out_insert = next(q for q in statements if "INSERT INTO transaction_outputs" in q)
        # Explicit on-chain index wins over the enumerate position.
        assert statements[out_insert][0][2] == 3
