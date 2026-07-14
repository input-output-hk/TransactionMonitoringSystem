"""ClickHouse schema: DDL templates, creation, layout guard, retention TTLs.

Split out of ``app.db.clickhouse`` (which retains client/executor state,
writers, and readers): everything in this module is a pure function of an
already-connected ``Client``, so it carries none of the connection plumbing
and the migration script can share the exact same DDL.

Schema v2: dedup-safe layout

All per-transaction fact tables are ReplacingMergeTree versioned by
ingestion_timestamp (set once by the ingester and shared by a tx's child
rows), keyed on the natural identity of each row. Ingestion replays after a
crash/restart or checkpoint-driven re-sync therefore collapse to one row per
key instead of accumulating duplicates that inflate sums and counts.

Deliberately NO PARTITION BY anywhere: ReplacingMergeTree only deduplicates
within a partition, and no time column is GUARANTEED stable across replays
(`timestamp` is chain time derived from the block slot, but falls back to
ingestion wall clock when the era summaries are unavailable, so a replay
can re-stamp it; `ingestion_timestamp` and `analyzed_at` move on every
replay/re-score). A time-based partition would scatter versions of the
same logical row across partitions where neither background merges nor
FINAL can ever collapse them.

The templates are shared with backend/scripts/migrate_dedup_schema.py (which
instantiates them as `<table>__mig` before swapping), so the migrated layout
cannot drift from what create_all() builds on a fresh install.
"""

import logging

from clickhouse_driver import Client
from clickhouse_driver.errors import Error as ClickHouseError

from app.config import settings

logger = logging.getLogger(__name__)

# Column list for the transactions time-ordered projection: exactly the
# scalar columns the list/recent endpoints read, plus the RMT version column.
# raw_data (the dominant bytes, ZSTD-compressed JSON) and metadata are
# deliberately excluded: projecting them doubled the storage and merge IO of
# the largest table for queries that never select them. Shared between the
# CREATE TABLE DDL and the in-place migration so the two cannot drift.
_TX_PROJECTION_SELECT = (
    "SELECT tx_hash, network, slot, block_height, block_hash, block_index, "
    "timestamp, fee, deposit, input_count, output_count, total_input_value, "
    "total_output_value, addresses, ingestion_timestamp "
    "ORDER BY network, timestamp"
)

SCHEMA_DDL: dict[str, str] = {
    # Main transactions table. ORDER BY (network, tx_hash) is the dedup key;
    # the p_by_time_v2 projection re-sorts by (network, timestamp) so the list
    # endpoint's top-N-by-time query stays a tail read instead of a full sort.
    # raw_data is ZSTD(3)-compressed (large JSON compresses ~5-10x) and
    # raw_data_truncated flags payloads dropped under RAW_DATA_MAX_BYTES —
    # an invalid sliced-JSON prefix is never stored.
    "transactions": """
        CREATE TABLE IF NOT EXISTS {table} (
            tx_hash String,
            network String,
            slot Nullable(UInt64),
            block_height Nullable(UInt32),
            block_hash Nullable(String),
            block_index Nullable(UInt32),
            timestamp DateTime,
            fee UInt64,
            deposit Nullable(Int64),
            input_count UInt16,
            output_count UInt16,
            total_input_value Nullable(UInt64),
            total_output_value UInt64,
            addresses Array(String),
            metadata String,
            raw_data String CODEC(ZSTD(3)),
            raw_data_truncated UInt8 DEFAULT 0,
            script_valid UInt8 DEFAULT 1,
            ingestion_timestamp DateTime DEFAULT now(),
            INDEX idx_tx_hash tx_hash TYPE bloom_filter GRANULARITY 1,
            INDEX idx_network network TYPE bloom_filter GRANULARITY 1,
            INDEX idx_slot slot TYPE minmax GRANULARITY 1,
            INDEX idx_block_height block_height TYPE minmax GRANULARITY 1,
            INDEX idx_timestamp timestamp TYPE minmax GRANULARITY 1,
            PROJECTION p_by_time_v2 ("""
    + _TX_PROJECTION_SELECT
    + """)
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (network, tx_hash)
        SETTINGS deduplicate_merge_projection_mode = 'rebuild',
                 lightweight_mutation_projection_mode = 'rebuild'
    """,
    "transaction_inputs": """
        CREATE TABLE IF NOT EXISTS {table} (
            tx_hash String,
            network String,
            input_index UInt16,
            input_tx_hash String,
            input_index_in_tx UInt16,
            address String,
            amount UInt64,
            assets String,
            is_reference UInt8,
            is_collateral UInt8,
            is_unspent_attempt UInt8 DEFAULT 0,
            ingestion_timestamp DateTime DEFAULT now(),
            INDEX idx_tx_hash tx_hash TYPE bloom_filter GRANULARITY 1,
            INDEX idx_network network TYPE bloom_filter GRANULARITY 1,
            INDEX idx_address address TYPE bloom_filter GRANULARITY 1
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (network, tx_hash, input_index)
    """,
    "transaction_outputs": """
        CREATE TABLE IF NOT EXISTS {table} (
            tx_hash String,
            network String,
            output_index UInt16,
            address String,
            amount UInt64,
            assets String,
            is_collateral UInt8,
            ingestion_timestamp DateTime DEFAULT now(),
            INDEX idx_tx_hash tx_hash TYPE bloom_filter GRANULARITY 1,
            INDEX idx_network network TYPE bloom_filter GRANULARITY 1,
            INDEX idx_address address TYPE bloom_filter GRANULARITY 1
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (network, tx_hash, output_index)
    """,
    # Address lookup table (target of address_transactions_mv). The MV fires
    # on every INSERT into transactions, so replayed blocks produce duplicate
    # MV rows with identical keys — the ReplacingMergeTree collapses them.
    "address_transactions": """
        CREATE TABLE IF NOT EXISTS {table} (
            network     String,
            address     String,
            slot        UInt64,
            tx_hash     String,
            timestamp   DateTime,
            ingestion_timestamp DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (network, address, slot, tx_hash)
    """,
    # Extended UTxO-level features, populated inline during ingestion.
    # One row per output per transaction.
    "utxo_features": """
        CREATE TABLE IF NOT EXISTS {table} (
            tx_hash              String,
            network              String,
            output_index         UInt16,
            address              String,
            is_script_address    UInt8,
            ada_amount           UInt64,
            value_cbor_bytes     UInt32,
            unique_policy_count  UInt16,
            unique_token_count   UInt16,
            datum_present        UInt8,
            datum_bytes          UInt32,
            datum_ratio          Float32,
            utxo_total_bytes     UInt32,
            ingestion_timestamp  DateTime DEFAULT now(),
            INDEX idx_tx_hash    tx_hash TYPE bloom_filter GRANULARITY 1,
            INDEX idx_network    network TYPE bloom_filter GRANULARITY 1,
            INDEX idx_address    address TYPE bloom_filter GRANULARITY 1,
            INDEX idx_is_script  is_script_address TYPE minmax GRANULARITY 1
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (network, tx_hash, output_index)
    """,
    # Transaction-level script execution features, populated inline
    # during ingestion.  One row per transaction.
    "tx_script_features": """
        CREATE TABLE IF NOT EXISTS {table} (
            tx_hash              String,
            network              String,
            redeemers_count      UInt16,
            spending_inputs      UInt16,
            exunits_mem_total    UInt64,
            exunits_cpu_total    UInt64,
            mint_policy_count    UInt16,
            mint_entries         String,
            ingestion_timestamp  DateTime DEFAULT now(),
            INDEX idx_tx_hash    tx_hash TYPE bloom_filter GRANULARITY 1,
            INDEX idx_network    network TYPE bloom_filter GRANULARITY 1
        ) ENGINE = ReplacingMergeTree(ingestion_timestamp)
        ORDER BY (network, tx_hash)
    """,
    # Multi-class scoring output.  One row per scored transaction.
    # Each attack class gets an independent 0-100 score; -1 means the
    # gate condition failed (class not applicable). Versioned by analyzed_at
    # so a re-score replaces the prior row; no partition, so cross-day
    # re-scores of the same tx still merge.
    "tx_class_scores": """
        CREATE TABLE IF NOT EXISTS {table} (
            tx_hash          String,
            network          String,
            token_dust       Float32 DEFAULT -1,
            large_value      Float32 DEFAULT -1,
            large_datum      Float32 DEFAULT -1,
            multiple_sat     Float32 DEFAULT -1,
            front_running    Float32 DEFAULT -1,
            sandwich         Float32 DEFAULT -1,
            circular         Float32 DEFAULT -1,
            fake_token       Float32 DEFAULT -1,
            phishing         Float32 DEFAULT -1,
            max_score        Float32,
            max_class        String,
            risk_band        String,
            sub_scores       String,
            evidence         String DEFAULT '{{}}',
            corroboration_count   UInt8 DEFAULT 0,
            corroborating_classes String DEFAULT '',
            analysis_version String,
            analyzed_at      DateTime,
            INDEX idx_risk_band  risk_band TYPE bloom_filter GRANULARITY 1,
            INDEX idx_max_class  max_class TYPE bloom_filter GRANULARITY 1,
            INDEX idx_analyzed   analyzed_at TYPE minmax GRANULARITY 1
        ) ENGINE = ReplacingMergeTree(analyzed_at)
        ORDER BY (network, tx_hash)
    """,
    # Admin-curated archive of flagged transactions known to be false
    # positives. Additive to tx_class_scores: a row here suppresses the
    # corresponding score from "currently dangerous" lists at query time.
    # An entry can exist without a matching tx_class_scores row (cross-
    # instance CSV import: another admin's archive for a tx this instance
    # never observed).
    "archived_alerts": """
        CREATE TABLE IF NOT EXISTS {table} (
            tx_hash      String,
            network      String,
            note         String,
            archived_by  String,
            archived_at  DateTime DEFAULT now(),
            source       String DEFAULT 'local',
            INDEX idx_tx_hash    tx_hash     TYPE bloom_filter GRANULARITY 1,
            INDEX idx_network    network     TYPE bloom_filter GRANULARITY 1,
            INDEX idx_archived   archived_at TYPE minmax       GRANULARITY 1
        ) ENGINE = ReplacingMergeTree(archived_at)
        ORDER BY (network, tx_hash)
    """,
}

# ReplacingMergeTree (dedup key columns, version column) per v2 table. Used by
# scripts/migrate_dedup_schema.py to build the argMax() collapse queries; kept
# adjacent to SCHEMA_DDL so the two cannot drift.
DEDUP_TABLE_KEYS: dict[str, tuple[tuple[str, ...], str]] = {
    "transactions": (("network", "tx_hash"), "ingestion_timestamp"),
    "transaction_inputs": (("network", "tx_hash", "input_index"), "ingestion_timestamp"),
    "transaction_outputs": (("network", "tx_hash", "output_index"), "ingestion_timestamp"),
    "address_transactions": (("network", "address", "slot", "tx_hash"), "ingestion_timestamp"),
    "utxo_features": (("network", "tx_hash", "output_index"), "ingestion_timestamp"),
    "tx_script_features": (("network", "tx_hash"), "ingestion_timestamp"),
    "tx_class_scores": (("network", "tx_hash"), "analyzed_at"),
    "archived_alerts": (("network", "tx_hash"), "archived_at"),
}

# Path of the one-shot migration named in the startup-guard error message.
_MIGRATION_SCRIPT = "backend/scripts/migrate_dedup_schema.py"

# Live column types that MUST match the DDL for ingestion to be safe.
# History: per-transaction counts and indexes were UInt8 until a real preprod
# transaction with more than 255 inputs overflowed them and wedged chain sync
# (insert fails, checkpoint never advances). The protocol max transaction
# size (16384 bytes on mainnet/preprod) admits roughly 400+ minimal inputs,
# so UInt8 was never a valid bound; UInt16 is, with ample headroom. These
# columns sit in ORDER BY keys and the transactions projection, so they
# cannot be widened with ALTER MODIFY COLUMN — a mismatch requires the same
# rebuild-and-exchange migration as a legacy engine.
WIDE_COUNT_COLUMNS: dict[str, dict[str, str]] = {
    "transactions": {"input_count": "UInt16", "output_count": "UInt16"},
    "transaction_inputs": {"input_index": "UInt16", "input_index_in_tx": "UInt16"},
    "transaction_outputs": {"output_index": "UInt16"},
}


def stale_count_columns(client: Client, table: str) -> list[str]:
    """Return WIDE_COUNT_COLUMNS entries whose live type mismatches the DDL.

    Empty list for tables that don't exist (created fresh from SCHEMA_DDL)
    or that have no enforced columns.
    """
    expected = WIDE_COUNT_COLUMNS.get(table)
    if not expected:
        return []
    rows = client.execute(
        """
        SELECT name, type FROM system.columns
        WHERE database = currentDatabase() AND table = %(t)s
        """,
        {"t": table},
    )
    live = dict(rows)
    return sorted(col for col, want in expected.items() if col in live and live[col] != want)


def assert_no_legacy_schema(client: Client) -> None:
    """Refuse to start against a half-migrated (pre-v2) ClickHouse layout.

    CREATE TABLE IF NOT EXISTS silently keeps a legacy table's engine and
    partitioning, so without this check an un-migrated deployment would run
    with duplicate-accumulating MergeTree tables while the readers assume
    ReplacingMergeTree dedup. A v2 table is detected as: ReplacingMergeTree
    engine AND no PARTITION BY clause (any time-based partition is unstable
    across replays and breaks FINAL dedup). Tables that don't exist yet are
    fine — create_all() just created them from SCHEMA_DDL.

    Also refuses narrow count/index columns (WIDE_COUNT_COLUMNS): they make
    every 256+-input transaction — exactly the attack-shaped traffic this
    system exists to score — an unrecordable insert error that wedges chain
    sync at that block forever.
    """
    rows = client.execute(
        """
        SELECT name, engine, engine_full
        FROM system.tables
        WHERE database = currentDatabase()
          AND name IN %(tables)s
        """,
        {"tables": list(DEDUP_TABLE_KEYS)},
    )
    legacy = sorted(
        name
        for name, engine, engine_full in rows
        if engine != "ReplacingMergeTree" or "PARTITION BY" in (engine_full or "")
    )
    if legacy:
        raise RuntimeError(
            f"ClickHouse tables {legacy} still use the legacy (pre-dedup) "
            f"schema. Stop ALL app instances sharing this database and run "
            f"{_MIGRATION_SCRIPT} before starting again."
        )
    narrow = sorted(
        f"{table}.{col}"
        for table in WIDE_COUNT_COLUMNS
        for col in stale_count_columns(client, table)
    )
    if narrow:
        raise RuntimeError(
            f"ClickHouse columns {narrow} are narrower than the schema "
            f"requires (256+-input/output transactions would fail to "
            f"ingest). Stop ALL app instances sharing this database and "
            f"run {_MIGRATION_SCRIPT} before starting again."
        )


# table -> the settings knob holding its retention window in days.
# tx_class_scores, archived_alerts, and baselines are deliberately absent:
# they are the product (O(1) per tx) and are never expired.
_RETENTION_TABLE_KNOBS: tuple[tuple[str, str], ...] = (
    ("transactions", "CH_RETENTION_DAYS_TRANSACTIONS"),
    ("transaction_inputs", "CH_RETENTION_DAYS_IO"),
    ("transaction_outputs", "CH_RETENTION_DAYS_IO"),
    ("address_transactions", "CH_RETENTION_DAYS_IO"),
    ("utxo_features", "CH_RETENTION_DAYS_FEATURES"),
    ("tx_script_features", "CH_RETENTION_DAYS_FEATURES"),
)

# Retention knob -> what a short window silently breaks. Every baseline
# percentile query INNER JOINs transactions for chain-time windowing, and
# enrichment resolves PARENT transactions of any age (dormant-funds attacks
# spend arbitrarily old UTxOs), so retention on these tables degrades
# detection inputs, not just history. Loud warning, not a hard refusal: an
# operator may intentionally run a shorter horizon on a constrained box.
_RETENTION_BASELINE_COUPLING: dict[str, str] = {
    "CH_RETENTION_DAYS_FEATURES": ("baselines will be computed from a truncated population"),
    "CH_RETENTION_DAYS_TRANSACTIONS": (
        "the baseline percentile queries INNER JOIN transactions for "
        "chain-time windowing, so feature rows older than this silently "
        "vanish from percentiles AND sample counts; enrichment also "
        "resolves parent txs of any age from this table"
    ),
    "CH_RETENTION_DAYS_IO": (
        "enrichment resolves input addresses/amounts from "
        "transaction_inputs/outputs for parent txs of any age; older "
        "parents lose resolution"
    ),
}


def _baseline_window_days() -> int:
    # Lazy import: app.analysis.scorer_config -> normalise -> app.db.clickhouse
    # -> this module would be a cycle at module load (same pattern as
    # normalise._min_baseline_spread_ratio). Single source of truth is
    # baselines.windows.global_days in config/detection.yaml.
    from app.analysis.scorer_config import baselines_config

    return int(baselines_config()["windows"]["global_days"])


def _remove_table_ttl(client: Client, table: str) -> None:
    """Drop a previously applied retention TTL when its knob returns to 0.

    Without this, "0 = keep forever" was false after one enable/disable
    cycle: the old TTL clause survived on the table and kept deleting rows
    forever (review finding). REMOVE TTL raises on a table that never had
    one, so the live DDL is checked first (table-level TTLs only; none of
    our tables use column TTLs, so the substring witness is sufficient).
    """
    rows = client.execute(
        "SELECT create_table_query FROM system.tables "
        "WHERE database = currentDatabase() AND name = %(t)s",
        {"t": table},
    )
    if not rows or " TTL " not in rows[0][0]:
        return
    try:
        client.execute(f"ALTER TABLE {table} REMOVE TTL")
        logger.info("Retention TTL removed: %s (knob set to 0)", table)
    except ClickHouseError:
        # Another instance may have removed it concurrently; never block
        # startup over TTL housekeeping.
        logger.warning("Failed to remove retention TTL on %s", table, exc_info=True)


def apply_retention_ttls(client: Client) -> None:
    """Apply opt-in row TTLs from the CH_RETENTION_DAYS_* knobs (0 = off).

    TTL merges expire rows without partitions. Idempotent: MODIFY TTL
    replaces any prior clause; a knob back at 0 removes any stale clause.
    """
    window_days = _baseline_window_days()
    warned_knobs = set()
    for table, knob in _RETENTION_TABLE_KNOBS:
        days = int(getattr(settings, knob))
        if days <= 0:
            _remove_table_ttl(client, table)
            continue
        if knob in _RETENTION_BASELINE_COUPLING and days < window_days and knob not in warned_knobs:
            warned_knobs.add(knob)
            logger.warning(
                "%s=%d is below the %d-day global baseline window; %s.",
                knob,
                days,
                window_days,
                _RETENTION_BASELINE_COUPLING[knob],
            )
        client.execute(f"ALTER TABLE {table} MODIFY TTL ingestion_timestamp + INTERVAL {days} DAY")
        logger.info("Retention TTL applied: %s expires after %d days", table, days)


def migrate_transactions_projection(client: Client) -> None:
    """One-time in-place swap of p_by_time (SELECT *) for the narrowed
    p_by_time_v2 on existing deployments.

    Gated on the live CREATE TABLE text so MATERIALIZE (a part-rewriting
    mutation) runs once, not on every boot. Idempotent and
    concurrency-tolerant (IF [NOT] EXISTS on every statement). Between DROP
    and the MATERIALIZE completing, list queries fall back to the base table:
    slower, still correct.
    """
    rows = client.execute(
        "SELECT create_table_query FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'transactions'"
    )
    if rows and "p_by_time_v2" in rows[0][0]:
        return
    # ClickHouse >= 24.7 refuses projections on a ReplacingMergeTree unless
    # the table declares how dedup merges treat them, and (verified live on
    # 26.1.3) gates lightweight DELETE on the TABLE-level
    # lightweight_mutation_projection_mode — the query-level setting is
    # ignored there. 'rebuild' recomputes the projection from surviving rows
    # (the only mode that keeps it correct). Must be set before
    # ADD PROJECTION on existing tables.
    client.execute(
        "ALTER TABLE transactions "
        "MODIFY SETTING deduplicate_merge_projection_mode = 'rebuild', "
        "lightweight_mutation_projection_mode = 'rebuild'"
    )
    client.execute("ALTER TABLE transactions DROP PROJECTION IF EXISTS p_by_time")
    client.execute(
        "ALTER TABLE transactions ADD PROJECTION IF NOT EXISTS p_by_time_v2 ("
        + _TX_PROJECTION_SELECT
        + ")"
    )
    # Async mutation; old parts gain the projection as it runs, new inserts
    # build it inline.
    client.execute("ALTER TABLE transactions MATERIALIZE PROJECTION p_by_time_v2")
    logger.info("transactions projection migrated: p_by_time -> p_by_time_v2")


def _create_transactions(client: Client) -> None:
    """Main transactions table + its one-off column migrations. MUST run before
    migrate_transactions_projection (the v2 projection SELECT references the
    network column added here)."""
    # Main transactions table (see SCHEMA_DDL for the layout rationale).
    client.execute(SCHEMA_DDL["transactions"].format(table="transactions"))

    # Add network column if it doesn't exist (migration for existing tables)
    try:
        client.execute(
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS network String DEFAULT 'preprod'"
        )
    except Exception as e:
        logger.debug(f"Network column may already exist or migration not needed: {e}")

    # Add block_index column if it doesn't exist (migration for existing tables)
    try:
        client.execute(
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS block_index Nullable(UInt32)"
        )
    except Exception as e:
        logger.debug(f"block_index column may already exist or migration not needed: {e}")

    # Migrate total_input_value from UInt64 (old default 0 = ambiguous) to
    # Nullable(UInt64) so that NULL = "unresolved" and 0 = "known zero".
    try:
        client.execute("ALTER TABLE transactions MODIFY COLUMN total_input_value Nullable(UInt64)")
    except Exception as e:
        logger.debug(f"total_input_value already Nullable or migration not needed: {e}")

    # Phase-2 validation outcome (migration for existing tables): DEFAULT 1
    # because historical rows predate failed-tx ingestion semantics.
    client.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS script_valid UInt8 DEFAULT 1")

    # Serialized (CBOR) transaction byte size, consumed as a shape feature by the
    # optional clustering sidecar's HostBackedRepo. Additive and default-0 (a
    # near-zero permanent cost), so it is unconditional rather than flag-gated:
    # that avoids the migration-vs-writer ordering hazard a gated column creates.
    # The ingester only populates it when the Ogmios chain-sync request carries
    # CBOR (includeCbor); without it the column stays 0 and the sidecar's size
    # feature is a constant (RobustScaler-collapsed, degraded not broken).
    client.execute(
        "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tx_size_bytes UInt32 DEFAULT 0"
    )


def _create_transaction_io(client: Client) -> None:
    """Transaction inputs/outputs tables + their column migrations."""
    # Transaction inputs table
    client.execute(SCHEMA_DDL["transaction_inputs"].format(table="transaction_inputs"))

    # Add network column if it doesn't exist (migration for existing tables)
    try:
        client.execute(
            "ALTER TABLE transaction_inputs ADD COLUMN IF NOT EXISTS network String DEFAULT 'preprod'"
        )
    except Exception as e:
        logger.debug(f"Network column may already exist or migration not needed: {e}")

    # Attempted-spend flag for failed txs (migration for existing tables).
    client.execute(
        "ALTER TABLE transaction_inputs ADD COLUMN IF NOT EXISTS is_unspent_attempt UInt8 DEFAULT 0"
    )

    # Transaction outputs table
    client.execute(SCHEMA_DDL["transaction_outputs"].format(table="transaction_outputs"))

    # Add network column if it doesn't exist (migration for existing tables)
    try:
        client.execute(
            "ALTER TABLE transaction_outputs ADD COLUMN IF NOT EXISTS network String DEFAULT 'preprod'"
        )
    except Exception as e:
        logger.debug(f"Network column may already exist or migration not needed: {e}")


def _create_address_lookup(client: Client) -> None:
    """Address lookup table, its populating materialized view, and the one-time
    backfill from existing transactions.

    `addresses Array(String)` in `transactions` carries all input + output
    addresses for a transaction.  A bloom_filter skip index on that column
    helps at the granule level, but `has(addresses, ?)` still scans every
    row in passing granules — at scale this degrades to a full-partition scan.

    This table normalises the array into one row per (address, tx_hash) pair,
    ordered by (network, address, slot) so every address lookup is a B-tree
    point seek that prunes to a tiny number of granules.

    NOTE: `transactions.slot` is Nullable; `ifNull(slot, 0)` is used so the
    lookup table column stays non-nullable and the ORDER BY is efficient.
    """
    client.execute(SCHEMA_DDL["address_transactions"].format(table="address_transactions"))

    # Materialized view: unnests `addresses` into address_transactions.
    # ARRAY JOIN in the SELECT is supported in ClickHouse MV definitions and
    # fires on every INSERT into `transactions`.
    client.execute("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS address_transactions_mv
        TO address_transactions
        AS SELECT
            network,
            addr                  AS address,
            ifNull(slot, 0)       AS slot,
            tx_hash,
            timestamp,
            ingestion_timestamp
        FROM transactions
        ARRAY JOIN addresses AS addr
        WHERE notEmpty(addr)
    """)

    # Backfill existing rows (no-op on a fresh deployment where the table is
    # already empty; on existing deployments this seeds the lookup table once).
    try:
        client.execute("""
            INSERT INTO address_transactions (network, address, slot, tx_hash, timestamp, ingestion_timestamp)
            SELECT
                network,
                addr                AS address,
                ifNull(slot, 0)     AS slot,
                tx_hash,
                timestamp,
                ingestion_timestamp
            FROM transactions
            ARRAY JOIN addresses AS addr
            WHERE notEmpty(addr)
              AND (network, addr, ifNull(slot, 0), tx_hash)
                  NOT IN (SELECT network, address, slot, tx_hash FROM address_transactions)
        """)
    except Exception as e:
        logger.debug(f"address_transactions backfill skipped: {e}")


def _create_detection_tables(client: Client) -> None:
    """Multi-class detection tables: UTxO/script features, the scoring output
    (+ its column migrations), and the admin archive."""
    # Extended UTxO-level features, populated inline during ingestion.
    # One row per output per transaction.
    client.execute(SCHEMA_DDL["utxo_features"].format(table="utxo_features"))

    # Transaction-level script execution features, populated inline
    # during ingestion.  One row per transaction.
    client.execute(SCHEMA_DDL["tx_script_features"].format(table="tx_script_features"))

    # Multi-class scoring output.  One row per scored transaction.
    client.execute(SCHEMA_DDL["tx_class_scores"].format(table="tx_class_scores"))

    client.execute(
        "ALTER TABLE tx_class_scores ADD COLUMN IF NOT EXISTS evidence String DEFAULT '{}'"
    )
    # Cross-class corroboration flag (additive; see engine.py). Existing
    # rows default to 0 / '' until re-scored.
    client.execute(
        "ALTER TABLE tx_class_scores ADD COLUMN IF NOT EXISTS corroboration_count UInt8 DEFAULT 0"
    )
    client.execute(
        "ALTER TABLE tx_class_scores "
        "ADD COLUMN IF NOT EXISTS corroborating_classes String DEFAULT ''"
    )

    # Admin-curated archive of flagged transactions (see SCHEMA_DDL).
    client.execute(SCHEMA_DDL["archived_alerts"].format(table="archived_alerts"))


def _create_baselines(client: Client) -> None:
    """Baseline statistics table and the append-only held-drift event log
    (+ its column migrations)."""
    # Per-script / per-policy / global baseline statistics used by the
    # percentile normalisation framework.  Updated daily (or on bootstrap).
    #
    # The ``network`` column is part of the ORDER BY key so ReplacingMergeTree
    # deduplicates within a network and preprod / preview / mainnet baselines
    # cannot overwrite each other. If the legacy (network-less) table exists,
    # drop it so the new schema applies — baselines are always recomputable
    # from utxo_features / tx_script_features.
    try:
        cols = client.execute(
            "SELECT name FROM system.columns "
            "WHERE database = currentDatabase() AND table = 'baselines'"
        )
        if cols and not any(row[0] == "network" for row in cols):
            client.execute("DROP TABLE IF EXISTS baselines")
            logger.info("Dropped legacy baselines table (pre-network schema)")
    except ClickHouseError as e:
        # Concurrent startup of another app instance may have already dropped
        # the table; log and continue rather than mask the error silently.
        logger.warning("Legacy baselines check/drop skipped: %s", e)
    client.execute("""
        CREATE TABLE IF NOT EXISTS baselines (
            network      String,
            scope_type   String,
            scope_id     String,
            feature      String,
            p50          Float64,
            p99          Float64,
            sample_count UInt64,
            computed_at  DateTime,
            window_days  UInt16
        ) ENGINE = ReplacingMergeTree(computed_at)
        ORDER BY (network, scope_type, scope_id, feature)
    """)

    # Append-only log of HELD baseline updates. When a recompute produces
    # a p99 drifting beyond baselines.drift.p99_threshold from the stored
    # value, the update is NOT applied (the prior row stays active) and
    # the rejected candidate is recorded here for analyst review. This is
    # the anti-poisoning control: an attacker widening a per-script
    # distribution to de-sensitise a scorer must now move p99 slowly
    # under the drift threshold, and every held jump leaves a trail.
    client.execute("""
        CREATE TABLE IF NOT EXISTS baseline_drift_events (
            network     String,
            scope_type  String,
            scope_id    String,
            feature     String,
            old_p99     Float64,
            new_p99     Float64,
            drift_ratio Float64,
            detected_at DateTime,
            axis        String DEFAULT 'p99',
            applied     UInt8 DEFAULT 0
        ) ENGINE = MergeTree
        ORDER BY (network, detected_at)
    """)
    # Direction-aware drift guard additions (migration for existing tables):
    # axis = which percentile drifted (legacy old_p99/new_p99 columns carry
    # that axis's values); applied = recall-safe drift inserted anyway.
    client.execute(
        "ALTER TABLE baseline_drift_events ADD COLUMN IF NOT EXISTS axis String DEFAULT 'p99'"
    )
    client.execute(
        "ALTER TABLE baseline_drift_events ADD COLUMN IF NOT EXISTS applied UInt8 DEFAULT 0"
    )


def create_all(client: Client) -> None:
    """Create every table, the address MV, run one-off column migrations,
    verify the layout is v2, and apply retention TTLs.

    Decomposed into per-table helpers; the call ORDER below is load-bearing and
    must be preserved (see the projection note). Raises ClickHouseError on
    failure (the caller owns logging/handling).
    """
    _create_transactions(client)

    # Swap the legacy SELECT * projection for the narrowed v2 (no-op once
    # done). Must run AFTER _create_transactions: the v2 projection SELECT
    # references the network column, so on a pre-network-column deployment
    # running it first crashes startup with an unknown-column error before the
    # ADD COLUMN migration can fix the table.
    migrate_transactions_projection(client)

    _create_transaction_io(client)
    _create_address_lookup(client)
    _create_detection_tables(client)
    _create_baselines(client)

    # Startup guard: CREATE IF NOT EXISTS above silently keeps a legacy
    # table's engine/partitioning, so verify the live layout is v2 and
    # refuse to run half-migrated (raises RuntimeError naming the
    # migration script).
    assert_no_legacy_schema(client)

    # Opt-in retention TTLs (CH_RETENTION_DAYS_*, default 0 = forever).
    apply_retention_ttls(client)
