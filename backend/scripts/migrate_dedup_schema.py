#!/usr/bin/env python3
"""One-shot in-place migration to the dedup-safe (v2) ClickHouse schema.

WHY: the legacy fact tables were plain MergeTree (crash-replay duplicated
rows forever, double-counting the sums that feed scorers) and the
ReplacingMergeTree tables were partitioned by a time column that changes on
every re-score, so FINAL could never collapse cross-day duplicates. The v2
layout (see clickhouse.SCHEMA_DDL) fixes both; this script migrates the
EXISTING data into it, collapsing accumulated duplicates via argMax.

REQUIREMENTS
  - Stop ALL app instances sharing this ClickHouse database first (preprod
    and preview run against the same server). The script refuses to guard
    against concurrent writers; a write between collapse and swap would be
    lost.
  - Run with the same .env as the app:  cd backend && python
    scripts/migrate_dedup_schema.py            (dry-run: prints the plan)
  - Apply with:  python scripts/migrate_dedup_schema.py --apply

WHAT IT DOES, per table in clickhouse.DEDUP_TABLE_KEYS
  1. Skip if the live table is already v2 (ReplacingMergeTree, no partition)
     or does not exist (execute_schema creates it fresh) — idempotent.
  2. CREATE {table}__mig from clickhouse.SCHEMA_DDL (single source of truth).
  3. INSERT INTO {table}__mig SELECT <key cols>, argMax(<other cols>,
     <version>), max(<version>) FROM {table} GROUP BY <key cols> — one
     surviving row per logical key, newest version wins.
  4. Verify: count({table}__mig) == count of distinct keys in the legacy
     table. Abort (leaving the live table untouched) on mismatch.
  5. EXCHANGE TABLES (atomic) and rename the legacy data to
     {table}__legacy_<UTC date>. Kept for manual inspection; drop later.

Plus: drops the address_transactions_mv up front (it pins the pre-swap
table UUIDs) and the dead tx_analysis_results table (never written; see the
audit), then re-runs clickhouse.execute_schema() to recreate the MV and run
the v2 startup guard as the final verification.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import clickhouse  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_dedup_schema")

_MIG_SUFFIX = "__mig"


def _table_info(client, table: str):
    rows = client.execute(
        """
        SELECT engine, engine_full FROM system.tables
        WHERE database = currentDatabase() AND name = %(t)s
        """,
        {"t": table},
    )
    return rows[0] if rows else None


def _columns(client, table: str):
    rows = client.execute(
        """
        SELECT name FROM system.columns
        WHERE database = currentDatabase() AND table = %(t)s
        ORDER BY position
        """,
        {"t": table},
    )
    return [r[0] for r in rows]


def _is_v2(info) -> bool:
    engine, engine_full = info
    return engine == "ReplacingMergeTree" and "PARTITION BY" not in (engine_full or "")


def _distinct_key_count(client, table: str, key_cols) -> int:
    keys = ", ".join(key_cols)
    rows = client.execute(
        f"SELECT count() FROM (SELECT {keys} FROM {table} GROUP BY {keys})"
    )
    return int(rows[0][0])


def migrate_table(client, table: str, apply: bool, legacy_suffix: str) -> bool:
    """Migrate one table. Returns True when a swap happened."""
    info = _table_info(client, table)
    if info is None:
        logger.info("%s: does not exist; execute_schema will create it fresh", table)
        return False
    if _is_v2(info):
        logger.info("%s: already v2 (ReplacingMergeTree, no partition); skipping", table)
        return False

    key_cols, version_col = clickhouse.DEDUP_TABLE_KEYS[table]
    legacy_cols = _columns(client, table)
    distinct_keys = _distinct_key_count(client, table, key_cols)
    total_rows = int(client.execute(f"SELECT count() FROM {table}")[0][0])
    logger.info(
        "%s: legacy engine=%s rows=%d distinct_keys=%d (collapsing %d duplicates)",
        table, info[0], total_rows, distinct_keys, total_rows - distinct_keys,
    )
    if not apply:
        return False

    mig = f"{table}{_MIG_SUFFIX}"
    client.execute(f"DROP TABLE IF EXISTS {mig}")
    client.execute(clickhouse.SCHEMA_DDL[table].format(table=mig))

    # Insert only the columns the legacy table also has; v2-only columns
    # (e.g. raw_data_truncated) take their DEFAULT.
    v2_cols = _columns(client, mig)
    insert_cols = [c for c in v2_cols if c in legacy_cols]
    select_exprs = []
    for col in insert_cols:
        if col in key_cols:
            select_exprs.append(col)
        elif col == version_col:
            select_exprs.append(f"max({version_col}) AS {version_col}")
        else:
            select_exprs.append(f"argMax({col}, {version_col}) AS {col}")
    client.execute(
        f"INSERT INTO {mig} ({', '.join(insert_cols)}) "
        f"SELECT {', '.join(select_exprs)} FROM {table} "
        f"GROUP BY {', '.join(key_cols)}"
    )

    migrated = int(client.execute(f"SELECT count() FROM {mig}")[0][0])
    if migrated != distinct_keys:
        raise RuntimeError(
            f"{table}: migrated row count {migrated} != distinct key count "
            f"{distinct_keys}; live table untouched, {mig} left for inspection."
        )

    client.execute(f"EXCHANGE TABLES {table} AND {mig}")
    legacy_name = f"{table}__legacy_{legacy_suffix}"
    client.execute(f"RENAME TABLE {mig} TO {legacy_name}")
    logger.info(
        "%s: swapped to v2 (%d rows); legacy data preserved as %s",
        table, migrated, legacy_name,
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Perform the migration (default: dry-run that only reports counts).",
    )
    args = parser.parse_args()

    clickhouse.init_client()
    client = clickhouse._get_client()
    legacy_suffix = datetime.now(timezone.utc).strftime("%Y%m%d")

    db_engine = client.execute(
        "SELECT engine FROM system.databases WHERE name = currentDatabase()"
    )[0][0]
    if db_engine != "Atomic":
        raise RuntimeError(
            f"Database engine is {db_engine}; EXCHANGE TABLES requires Atomic. "
            "Migrate manually with paired RENAME TABLE statements."
        )

    if args.apply:
        # The MV pins its source/target table UUIDs; it must not survive the
        # swap. execute_schema() recreates it against the v2 tables below.
        client.execute("DROP VIEW IF EXISTS address_transactions_mv")
        # Dead table: defined by the legacy schema, never written by any code
        # path (audit finding); the v2 schema does not recreate it.
        client.execute("DROP TABLE IF EXISTS tx_analysis_results")

    swapped = 0
    for table in clickhouse.DEDUP_TABLE_KEYS:
        if migrate_table(client, table, args.apply, legacy_suffix):
            swapped += 1

    if args.apply:
        # Recreates the MV, creates any missing tables, and runs the v2
        # startup guard — the same checks the app performs at boot.
        clickhouse.execute_schema()
        logger.info("Migration complete: %d table(s) swapped. Schema verified v2.", swapped)
        logger.info(
            "Legacy tables are preserved with the __legacy_%s suffix; "
            "drop them manually after a verification window.", legacy_suffix,
        )
    else:
        logger.info("Dry-run complete. Re-run with --apply to migrate.")
    clickhouse.close_client()
    return 0


if __name__ == "__main__":
    sys.exit(main())
