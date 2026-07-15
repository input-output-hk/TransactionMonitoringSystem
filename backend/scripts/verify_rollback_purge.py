#!/usr/bin/env python3
"""Live verification that the rollback purge works on a real ClickHouse.

WHY: lightweight DELETE on a table with projections throws on ClickHouse
>= 24.7 unless lightweight_mutation_projection_mode is set, and every
persistence test mocks the client, so only a real server can prove the
purge path survives. On 26.x the gate reads the TABLE-level merge-tree
setting, which mocked tests cannot catch either, so this script also
exercises the legacy in-place projection migration end-to-end: a sentinel
database gets a pre-projection ``transactions`` table, runs
``migrate_transactions_projection`` against it, and then the purge. Run
this once against the deployed server version after any change to
delete_rolled_back_txs, migrate_transactions_projection, or the
transactions projection.

Usage:  cd backend && python scripts/verify_rollback_purge.py
        (requires a reachable ClickHouse, e.g. docker compose up -d clickhouse)

Uses a sentinel network and a sentinel database so real data is never
touched; both are cleaned up on every run. Exits non-zero on any failure.
"""

import logging
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clickhouse_driver import Client

from app.config import settings
from app.db import clickhouse, clickhouse_schema
from app.logging_utils import setup_logging
from app.models.transaction import NormalizedTransaction

setup_logging()
logger = logging.getLogger("verify_rollback_purge")

# Sentinel network: rows under it are created and deleted by this script only.
_VERIFY_NETWORK = "verify_purge"
_VERIFY_TX_HASH = "ff" * 32
_VERIFY_SLOT = 100
_ROLLBACK_SLOT = 50  # below _VERIFY_SLOT so the purge must select the row

# Sentinel database for the legacy-migration phase: created and dropped by
# this script only, so the deployment's real schema is never mutated.
_LEGACY_DB = "verify_purge_legacy"

# Pre-projection transactions layout: the column set the v2 projection SELECT
# references must exist (network included; the column migrations have already
# run on any deployment old enough to lack the projection), but there is no
# PROJECTION clause and no table-level projection settings. This is exactly
# the state migrate_transactions_projection is meant to upgrade in place.
_LEGACY_TRANSACTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS transactions (
        tx_hash String,
        network String,
        slot Nullable(UInt64),
        block_height Nullable(UInt32),
        block_hash Nullable(String),
        block_index Nullable(UInt32),
        timestamp DateTime,
        fee UInt64,
        deposit Nullable(Int64),
        input_count UInt8,
        output_count UInt8,
        total_input_value Nullable(UInt64),
        total_output_value UInt64,
        addresses Array(String),
        metadata String,
        raw_data String CODEC(ZSTD(3)),
        raw_data_truncated UInt8 DEFAULT 0,
        script_valid UInt8 DEFAULT 1,
        ingestion_timestamp DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
    ORDER BY (network, tx_hash)
"""


def _verify_current_schema_purge(client) -> int:
    """Phase 1: schema apply + purge against the configured database."""
    logger.info("Applying schema (runs the projection migration)...")
    clickhouse.execute_schema()

    ddl = client.execute(
        "SELECT create_table_query FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'transactions'"
    )[0][0]
    if "p_by_time_v2" not in ddl:
        logger.error("FAIL: p_by_time_v2 projection missing from transactions DDL")
        return 1
    if "PROJECTION p_by_time " in ddl or "p_by_time (" in ddl.replace("p_by_time_v2", ""):
        logger.error("FAIL: legacy p_by_time projection still present")
        return 1
    logger.info("OK: projection is p_by_time_v2, legacy p_by_time absent")

    # Clean any leftovers from a prior failed run, then insert the synthetic tx.
    clickhouse.delete_rolled_back_txs(_VERIFY_NETWORK, 0)
    tx = NormalizedTransaction(
        tx_hash=_VERIFY_TX_HASH,
        network=_VERIFY_NETWORK,
        slot=_VERIFY_SLOT,
        block_height=1,
        block_hash="aa" * 32,
        timestamp=datetime.now(UTC),
        fee=0,
        raw_data={},
    )
    clickhouse.insert_transactions_batch([tx])
    logger.info("Inserted synthetic tx under network=%s", _VERIFY_NETWORK)

    try:
        purged = clickhouse.delete_rolled_back_txs(_VERIFY_NETWORK, _ROLLBACK_SLOT)
    except Exception:
        logger.exception(
            "FAIL: rollback purge raised on a real server, so the crash-loop bug is NOT fixed"
        )
        return 1
    if len(purged) != 1:
        logger.error("FAIL: expected 1 purged tx, got %s", purged)
        return 1
    # The delayed second pass must run clean on the real server too.
    clickhouse.delete_score_rows(_VERIFY_NETWORK, purged)

    remaining = client.execute(
        "SELECT count() FROM transactions WHERE network = %(n)s",
        {"n": _VERIFY_NETWORK},
    )[0][0]
    if remaining != 0:
        logger.error("FAIL: %d rows survived the purge", remaining)
        return 1

    logger.info("OK: purge deleted the row without raising on the current schema")
    return 0


def _verify_legacy_migration_purge(admin_client) -> int:
    """Phase 2: legacy (pre-projection) deployment, migrated in place.

    Builds a sentinel database holding a ``transactions`` table WITHOUT the
    projection or the table-level projection settings, runs
    migrate_transactions_projection against it, asserts the projection and
    BOTH table settings landed, then runs the real purge path. This is the
    MODIFY SETTING -> DROP -> ADD -> MATERIALIZE sequence that mocked tests
    cannot validate (the 26.x projected-DELETE gate reads live table state).
    """
    admin_client.execute(f"DROP DATABASE IF EXISTS {_LEGACY_DB}")
    admin_client.execute(f"CREATE DATABASE {_LEGACY_DB}")
    legacy_client = Client(
        host=settings.CLICKHOUSE_HOST,
        port=settings.CLICKHOUSE_PORT,
        user=settings.CLICKHOUSE_USER,
        password=settings.CLICKHOUSE_PASSWORD,
        database=_LEGACY_DB,
        secure=False,
    )
    try:
        legacy_client.execute(_LEGACY_TRANSACTIONS_DDL)
        # The purge deletes from every cleanup table, so the sentinel DB
        # needs them all; only transactions uses the legacy layout.
        for table in clickhouse._ROLLBACK_CLEANUP_TABLES:
            if table == "transactions":
                continue
            legacy_client.execute(clickhouse_schema.SCHEMA_DDL[table].format(table=table))
        legacy_client.execute(
            "INSERT INTO transactions (tx_hash, network, slot, timestamp, fee) VALUES",
            [(_VERIFY_TX_HASH, _VERIFY_NETWORK, _VERIFY_SLOT, datetime.now(UTC), 0)],
        )
        logger.info("Built legacy (pre-projection) sentinel deployment in %s", _LEGACY_DB)

        clickhouse_schema.migrate_transactions_projection(legacy_client)

        ddl = legacy_client.execute(
            "SELECT create_table_query FROM system.tables "
            "WHERE database = currentDatabase() AND name = 'transactions'"
        )[0][0]
        failures = [
            witness
            for witness in (
                "p_by_time_v2",
                "deduplicate_merge_projection_mode",
                "lightweight_mutation_projection_mode",
            )
            if witness not in ddl
        ]
        if failures:
            logger.error("FAIL: legacy migration left the table without %s", failures)
            return 1
        logger.info("OK: legacy migration added the projection and table settings")

        # Run the REAL purge function against the migrated table: the
        # module client is thread-local, so point this thread's slot at the
        # sentinel database for the call and restore it after.
        original = getattr(clickhouse._thread_local, "client", None)
        clickhouse._thread_local.client = legacy_client
        try:
            purged = clickhouse.delete_rolled_back_txs(_VERIFY_NETWORK, _ROLLBACK_SLOT)
        except Exception:
            logger.exception(
                "FAIL: purge raised on the legacy-migrated table; the "
                "in-place migration does not survive the projected-DELETE gate"
            )
            return 1
        finally:
            clickhouse._thread_local.client = original
        if purged != [_VERIFY_TX_HASH]:
            logger.error("FAIL: expected [%s] purged, got %s", _VERIFY_TX_HASH, purged)
            return 1
        remaining = legacy_client.execute(
            "SELECT count() FROM transactions WHERE network = %(n)s",
            {"n": _VERIFY_NETWORK},
        )[0][0]
        if remaining != 0:
            logger.error(
                "FAIL: %d rows survived the purge on the migrated table",
                remaining,
            )
            return 1
        logger.info("OK: purge ran clean on the legacy-migrated table")
        return 0
    finally:
        # Self-cleaning regardless of outcome: the sentinel database never
        # outlives the run.
        try:
            admin_client.execute(f"DROP DATABASE IF EXISTS {_LEGACY_DB}")
        except Exception:
            logger.warning("Cleanup: failed to drop %s", _LEGACY_DB, exc_info=True)
        legacy_client.disconnect()


def main() -> int:
    clickhouse.init_client()
    client = clickhouse._get_client()

    rc = _verify_current_schema_purge(client)
    if rc != 0:
        return rc

    rc = _verify_legacy_migration_purge(client)
    if rc != 0:
        return rc

    logger.info("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
