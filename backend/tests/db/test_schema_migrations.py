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
