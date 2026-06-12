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
  1. Skip if the live table is already v2 (ReplacingMergeTree, no partition,
     count/index columns matching clickhouse.WIDE_COUNT_COLUMNS) or does not
     exist (execute_schema creates it fresh) — idempotent. Narrow UInt8-era
     count columns force a rebuild even on a v2 engine: they overflow on
     256+-input transactions and cannot be ALTERed in place (ORDER BY keys,
     transactions projection).
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

# Collapse-step resource bounds. The GROUP BY holds argMax state per distinct
# key — for transactions that means one full raw_data JSON string per key, so
# an unchunked pass over a production-sized table exhausts server memory.
# Every DEDUP_TABLE_KEYS key contains tx_hash, so hashing it into buckets
# never splits a dedup group; 16 buckets bound the working set to ~1/16th of
# the table under the compose 4 GiB ClickHouse mem_limit.
_DEFAULT_HASH_BUCKETS = 16
# Just under the compose 4 GiB container cap; the external-aggregation
# threshold spills GROUP BY state to disk at half the budget so the merge
# phase keeps headroom.
_DEFAULT_MAX_MEMORY_BYTES = 3_000_000_000


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


def migrate_table(
    client, table: str, apply: bool, legacy_suffix: str,
    buckets: int = _DEFAULT_HASH_BUCKETS,
    max_memory_bytes: int = _DEFAULT_MAX_MEMORY_BYTES,
) -> bool:
    """Migrate one table. Returns True when a swap happened."""
    mig = f"{table}{_MIG_SUFFIX}"
    info = _table_info(client, table)
    if info is None:
        logger.info("%s: does not exist; execute_schema will create it fresh", table)
        return False
    # A v2 table can still need a rebuild when its count/index columns are
    # narrower than the DDL (UInt8-era layout): those columns live in ORDER BY
    # keys and the transactions projection, so ALTER MODIFY cannot widen them.
    stale_cols = clickhouse.stale_count_columns(client, table)
    if _is_v2(info) and not stale_cols:
        # Crash-recovery: a crash between EXCHANGE and the legacy RENAME
        # leaves the post-swap legacy data stranded under the __mig name
        # (the live table is already v2, so re-runs skipped it forever).
        if apply and _table_info(client, mig) is not None:
            legacy_name = f"{table}__legacy_{legacy_suffix}"
            client.execute(f"RENAME TABLE {mig} TO {legacy_name}")
            logger.info(
                "%s: recovered stranded %s as %s (crash between EXCHANGE "
                "and RENAME on a prior run)", table, mig, legacy_name,
            )
        else:
            logger.info(
                "%s: already v2 (ReplacingMergeTree, no partition, wide "
                "count columns); skipping", table,
            )
        return False
    if stale_cols:
        logger.info("%s: needs rebuild — narrow column(s) %s", table, stale_cols)

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

    client.execute(f"DROP TABLE IF EXISTS {mig}")
    client.execute(clickhouse.SCHEMA_DDL[table].format(table=mig))

    # Insert only the columns the legacy table also has; v2-only columns
    # (e.g. raw_data_truncated) take their DEFAULT.
    v2_cols = _columns(client, mig)
    insert_cols = [c for c in v2_cols if c in legacy_cols]
    # No AS aliases on the aggregate expressions: the INSERT's explicit
    # column list maps positionally, and aliasing max(version) back to the
    # version column's own name makes the ClickHouse analyzer substitute
    # the alias into the sibling argMax(col, version) expressions, which it
    # then rejects as a nested aggregate (Code 184; reproduced on 26.1).
    select_exprs = []
    for col in insert_cols:
        if col in key_cols:
            select_exprs.append(col)
        elif col == version_col:
            select_exprs.append(f"max({version_col})")
        else:
            select_exprs.append(f"argMax({col}, {version_col})")
    # Chunked by tx_hash bucket so the GROUP BY working set is bounded;
    # every dedup key contains tx_hash, so a bucket never splits a group.
    # spill threshold: external aggregation kicks in at half the memory
    # budget so the merge phase keeps headroom (see _DEFAULT_* comments).
    external_group_by = max_memory_bytes // 2
    for bucket in range(buckets):
        client.execute(
            f"INSERT INTO {mig} ({', '.join(insert_cols)}) "
            f"SELECT {', '.join(select_exprs)} FROM {table} "
            f"WHERE cityHash64(tx_hash) %% %(buckets)s = %(bucket)s "
            f"GROUP BY {', '.join(key_cols)}",
            {"buckets": buckets, "bucket": bucket},
            settings={
                "max_memory_usage": max_memory_bytes,
                "max_bytes_before_external_group_by": external_group_by,
            },
        )
        logger.info("%s: collapsed bucket %d/%d", table, bucket + 1, buckets)

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
    parser.add_argument(
        "--buckets", type=int, default=_DEFAULT_HASH_BUCKETS,
        help="tx_hash buckets per collapse INSERT (bounds GROUP BY memory).",
    )
    parser.add_argument(
        "--max-memory-bytes", type=int, default=_DEFAULT_MAX_MEMORY_BYTES,
        help="Per-INSERT max_memory_usage; external GROUP BY spills at half.",
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
        if migrate_table(
            client, table, args.apply, legacy_suffix,
            buckets=args.buckets, max_memory_bytes=args.max_memory_bytes,
        ):
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
