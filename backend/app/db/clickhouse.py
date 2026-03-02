"""ClickHouse database connection and operations"""

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from clickhouse_driver import Client
from clickhouse_driver.errors import Error as ClickHouseError

from app.config import settings
from app.models.transaction import NormalizedTransaction

logger = logging.getLogger(__name__)

# Maximum byte-length of the raw_data JSON stored per transaction.
# Full Ogmios payloads for Plutus transactions can reach hundreds
# of kilobytes; at Mainnet scale this balloons ClickHouse storage and slows
# ingestion. 64 KiB is sufficient for debugging while keeping inserts fast.
_RAW_DATA_MAX_BYTES = 65_536

# Thread-local ClickHouse clients + 3-worker executor.
#
# clickhouse_driver.Client is NOT thread-safe (one TCP connection per
# instance). Previously a single-worker executor serialised all reads and
# writes, meaning a slow analytical query could stall ingestion inserts.
#
# Three dedicated workers allow ingestion (INSERT), API reads (SELECT), and
# the Analysis Engine to make progress concurrently without sharing a Client.
# Each worker holds its own thread-local Client created lazily on first use.
#
# execute_schema() / init_client() / close_client() are called on the main
# thread at startup/shutdown before executor tasks are scheduled — safe because
# no executor tasks are in flight at those points.
_thread_local = threading.local()
_ch_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="clickhouse")


def _get_client() -> Client:
    """Return the per-thread ClickHouse Client, creating it on first call."""
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = Client(
            host=settings.CLICKHOUSE_HOST,
            port=settings.CLICKHOUSE_PORT,
            user=settings.CLICKHOUSE_USER,
            password=settings.CLICKHOUSE_PASSWORD,
            database=settings.CLICKHOUSE_DB,
            secure=False,
        )
        _thread_local.client = client
    return client


def init_client():
    """Validate ClickHouse connectivity at startup.

    Called on the main thread before any executor tasks are scheduled.
    _get_client() eagerly opens a connection for this thread (used by
    execute_schema which runs immediately after).
    """
    try:
        _get_client()
        logger.info("ClickHouse client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize ClickHouse client: {e}")
        raise


def close_client():
    """Disconnect ClickHouse clients on all threads."""
    def _close_thread():
        client = getattr(_thread_local, "client", None)
        if client:
            client.disconnect()
            _thread_local.client = None

    # Close each executor-worker thread's client.
    # At shutdown no new tasks are submitted, so three tasks distribute one
    # per worker reliably.
    futures = [_ch_executor.submit(_close_thread) for _ in range(3)]
    for f in futures:
        try:
            f.result(timeout=5)
        except Exception:
            pass
    # Also close the startup/main-thread client created by execute_schema().
    _close_thread()
    logger.info("ClickHouse clients closed")


def shutdown_executor():
    """Drain pending ClickHouse work and shut down the executor.
    Called once at application shutdown, after background tasks are cancelled."""
    _ch_executor.shutdown(wait=True)


def execute_schema():
    """Create ClickHouse tables if they don't exist"""
    client = _get_client()

    try:
        # Main transactions table.
        # Partitioned by day so that "last N hours / last day" queries prune to
        # 1–2 partitions instead of scanning the entire current month.
        # NOTE: changing PARTITION BY on an existing table requires recreating it;
        # this DDL only takes effect on a fresh deployment.
        client.execute("""
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
                raw_data String,
                ingestion_timestamp DateTime DEFAULT now(),
                INDEX idx_tx_hash tx_hash TYPE bloom_filter GRANULARITY 1,
                INDEX idx_network network TYPE bloom_filter GRANULARITY 1,
                INDEX idx_slot slot TYPE minmax GRANULARITY 1,
                INDEX idx_block_height block_height TYPE minmax GRANULARITY 1,
                INDEX idx_timestamp timestamp TYPE minmax GRANULARITY 1
            ) ENGINE = MergeTree()
            ORDER BY (network, timestamp, tx_hash)
            PARTITION BY toYYYYMMDD(timestamp)
        """)

        # Add network column if it doesn't exist (migration for existing tables)
        try:
            client.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS network String DEFAULT 'preprod'")
        except Exception as e:
            logger.debug(f"Network column may already exist or migration not needed: {e}")

        # Add block_index column if it doesn't exist (migration for existing tables)
        try:
            client.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS block_index Nullable(UInt32)")
        except Exception as e:
            logger.debug(f"block_index column may already exist or migration not needed: {e}")

        # Migrate total_input_value from UInt64 (old default 0 = ambiguous) to
        # Nullable(UInt64) so that NULL = "unresolved" and 0 = "known zero".
        try:
            client.execute(
                "ALTER TABLE transactions MODIFY COLUMN total_input_value Nullable(UInt64)"
            )
        except Exception as e:
            logger.debug(f"total_input_value already Nullable or migration not needed: {e}")

        # Transaction inputs table
        client.execute("""
            CREATE TABLE IF NOT EXISTS transaction_inputs (
                tx_hash String,
                network String,
                input_index UInt8,
                input_tx_hash String,
                input_index_in_tx UInt8,
                address String,
                amount UInt64,
                assets String,
                is_reference UInt8,
                is_collateral UInt8,
                ingestion_timestamp DateTime DEFAULT now(),
                INDEX idx_tx_hash tx_hash TYPE bloom_filter GRANULARITY 1,
                INDEX idx_network network TYPE bloom_filter GRANULARITY 1,
                INDEX idx_address address TYPE bloom_filter GRANULARITY 1
            ) ENGINE = MergeTree()
            ORDER BY (network, tx_hash, input_index)
            PARTITION BY toYYYYMM(ingestion_timestamp)
        """)

        # Add network column if it doesn't exist (migration for existing tables)
        try:
            client.execute("ALTER TABLE transaction_inputs ADD COLUMN IF NOT EXISTS network String DEFAULT 'preprod'")
        except Exception as e:
            logger.debug(f"Network column may already exist or migration not needed: {e}")

        # Transaction outputs table
        client.execute("""
            CREATE TABLE IF NOT EXISTS transaction_outputs (
                tx_hash String,
                network String,
                output_index UInt8,
                address String,
                amount UInt64,
                assets String,
                is_collateral UInt8,
                ingestion_timestamp DateTime DEFAULT now(),
                INDEX idx_tx_hash tx_hash TYPE bloom_filter GRANULARITY 1,
                INDEX idx_network network TYPE bloom_filter GRANULARITY 1,
                INDEX idx_address address TYPE bloom_filter GRANULARITY 1
            ) ENGINE = MergeTree()
            ORDER BY (network, tx_hash, output_index)
            PARTITION BY toYYYYMM(ingestion_timestamp)
        """)

        # Add network column if it doesn't exist (migration for existing tables)
        try:
            client.execute("ALTER TABLE transaction_outputs ADD COLUMN IF NOT EXISTS network String DEFAULT 'preprod'")
        except Exception as e:
            logger.debug(f"Network column may already exist or migration not needed: {e}")

        # Analysis results table — written by the Analysis Engine.
        # ORDER BY (network, tx_hash) is the ReplacingMergeTree dedup key and must
        # not include analyzed_at. risk_level gets a bloom_filter skip index so
        # WHERE risk_level = 'HIGH' queries don't scan every granule for the network.
        # Partitioned by day (same rationale as transactions).
        client.execute("""
            CREATE TABLE IF NOT EXISTS tx_analysis_results (
                tx_hash String,
                network String,
                risk_score Float32,
                risk_level String,
                cluster_id UInt32,
                is_anomaly UInt8,
                anomaly_reasons Array(String),
                analysis_version String,
                analyzed_at DateTime,
                INDEX idx_risk_level risk_level TYPE bloom_filter GRANULARITY 1,
                INDEX idx_analyzed_at analyzed_at TYPE minmax GRANULARITY 1
            ) ENGINE = ReplacingMergeTree(analyzed_at)
            ORDER BY (network, tx_hash)
            PARTITION BY toYYYYMMDD(analyzed_at)
        """)

        # Add risk_level index to existing tables (no-op if already present).
        try:
            client.execute(
                "ALTER TABLE tx_analysis_results ADD INDEX idx_risk_level risk_level "
                "TYPE bloom_filter GRANULARITY 1"
            )
            client.execute("ALTER TABLE tx_analysis_results MATERIALIZE INDEX idx_risk_level")
        except Exception as e:
            logger.debug(f"risk_level index may already exist: {e}")

        try:
            client.execute(
                "ALTER TABLE tx_analysis_results ADD INDEX idx_analyzed_at analyzed_at "
                "TYPE minmax GRANULARITY 1"
            )
            client.execute("ALTER TABLE tx_analysis_results MATERIALIZE INDEX idx_analyzed_at")
        except Exception as e:
            logger.debug(f"analyzed_at index may already exist: {e}")

        # Address lookup table.
        #
        # `addresses Array(String)` in `transactions` carries all input + output
        # addresses for a transaction.  A bloom_filter skip index on that column
        # helps at the granule level, but `has(addresses, ?)` still scans every
        # row in passing granules — at scale this degrades to a full-partition scan.
        #
        # This table normalises the array into one row per (address, tx_hash) pair,
        # ordered by (network, address, slot) so every address lookup is a B-tree
        # point seek that prunes to a tiny number of granules.
        #
        # The companion materialized view populates it automatically on every INSERT
        # into `transactions`.  A backfill query below seeds it from existing rows.
        #
        # NOTE: `transactions.slot` is Nullable; `ifNull(slot, 0)` is used so the
        # lookup table column stays non-nullable and the ORDER BY is efficient.
        client.execute("""
            CREATE TABLE IF NOT EXISTS address_transactions (
                network     String,
                address     String,
                slot        UInt64,
                tx_hash     String,
                timestamp   DateTime,
                ingestion_timestamp DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree()
            ORDER BY (network, address, slot, tx_hash)
            PARTITION BY toYYYYMMDD(timestamp)
        """)

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

        logger.info("ClickHouse schema initialized")
    except ClickHouseError as e:
        logger.error(f"Failed to create ClickHouse schema: {e}")
        raise


def insert_transactions_batch(transactions: List[NormalizedTransaction]):
    """Insert multiple transactions in a single batch per table.

    Always called via insert_transactions_batch_async (executor); the
    thread-local pool guarantees no concurrent ClickHouse access per client.
    """
    if not transactions:
        return

    client = _get_client()
    now = datetime.now(timezone.utc)

    try:
        client.execute(
            """
            INSERT INTO transactions (
                tx_hash, network, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
                input_count, output_count, total_input_value, total_output_value,
                addresses, metadata, raw_data, ingestion_timestamp
            ) VALUES
            """,
            [(
                tx.tx_hash,
                tx.network or settings.CARDANO_NETWORK,
                tx.slot,
                tx.block_height,
                tx.block_hash,
                tx.block_index,
                tx.timestamp,
                tx.fee,
                tx.deposit,
                tx.input_count,
                tx.output_count,
                tx.total_input_value,
                tx.total_output_value,
                tx.addresses,
                json.dumps(tx.metadata) if tx.metadata else "",
                (json.dumps(tx.raw_data) if tx.raw_data else "")[:_RAW_DATA_MAX_BYTES],
                tx.ingestion_timestamp or now,
            ) for tx in transactions]
        )

        all_inputs = [
            (
                tx.tx_hash,
                tx.network or settings.CARDANO_NETWORK,
                idx,
                inp.tx_hash,
                inp.index,
                inp.address,
                inp.amount,
                json.dumps(inp.assets) if inp.assets else "",
                1 if inp.is_reference else 0,
                1 if inp.is_collateral else 0,
                tx.ingestion_timestamp or now,
            )
            for tx in transactions
            for idx, inp in enumerate(tx.inputs)
        ]
        if all_inputs:
            client.execute(
                """
                INSERT INTO transaction_inputs (
                    tx_hash, network, input_index, input_tx_hash, input_index_in_tx,
                    address, amount, assets, is_reference, is_collateral, ingestion_timestamp
                ) VALUES
                """,
                all_inputs,
            )

        all_outputs = [
            (
                tx.tx_hash,
                tx.network or settings.CARDANO_NETWORK,
                idx,
                out.address,
                out.amount,
                json.dumps(out.assets) if out.assets else "",
                1 if out.is_collateral else 0,
                tx.ingestion_timestamp or now,
            )
            for tx in transactions
            for idx, out in enumerate(tx.outputs)
        ]
        if all_outputs:
            client.execute(
                """
                INSERT INTO transaction_outputs (
                    tx_hash, network, output_index, address, amount, assets, is_collateral, ingestion_timestamp
                ) VALUES
                """,
                all_outputs,
            )

        logger.debug(f"Inserted {len(transactions)} transactions into ClickHouse")
    except ClickHouseError as e:
        logger.error(f"Failed to insert transactions batch: {e}")
        raise


async def insert_transactions_batch_async(transactions: List[NormalizedTransaction]):
    """Non-blocking wrapper: runs insert_transactions_batch on the dedicated
    ClickHouse executor so it never blocks the event loop or the default pool."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_ch_executor, insert_transactions_batch, transactions)


# ---------------------------------------------------------------------------
# Analysis Engine helpers
# ---------------------------------------------------------------------------

def get_unanalyzed_transactions(network: str, batch_size: int) -> List[Dict[str, Any]]:
    """Return up to batch_size transactions that have no analysis result yet."""
    rows = _get_client().execute(
        """
        SELECT tx_hash, network, fee, input_count, output_count,
               total_output_value, metadata, addresses
        FROM transactions
        WHERE network = %(network)s
          AND tx_hash NOT IN (
              SELECT tx_hash FROM tx_analysis_results WHERE network = %(network)s
          )
        ORDER BY ingestion_timestamp ASC
        LIMIT %(batch_size)s
        """,
        {"network": network, "batch_size": batch_size},
    )
    keys = ("tx_hash", "network", "fee", "input_count", "output_count",
            "total_output_value", "metadata", "addresses")
    return [dict(zip(keys, row)) for row in rows]


def get_input_resolution(tx_hashes: List[str], network: str) -> Dict[str, Dict[str, Any]]:
    """Resolve input values and unique source addresses for a batch of transactions.

    Joins transaction_inputs against transaction_outputs on (input_tx_hash, input_index_in_tx).
    Only non-collateral, non-reference inputs are considered.
    Returns a dict keyed by tx_hash. Missing keys = no resolvable inputs (outputs pre-date sync).
    """
    if not tx_hashes:
        return {}
    rows = _get_client().execute(
        """
        SELECT
            ti.tx_hash,
            sum(coalesce(to2.amount, 0))  AS resolved_input_value,
            uniqExact(to2.address)        AS unique_input_addresses
        FROM transaction_inputs ti
        LEFT JOIN transaction_outputs to2
            ON  ti.input_tx_hash     = to2.tx_hash
            AND ti.input_index_in_tx = to2.output_index
            AND ti.network           = to2.network
        WHERE ti.tx_hash      IN %(tx_hashes)s
          AND ti.network       = %(network)s
          AND ti.is_collateral = 0
          AND ti.is_reference  = 0
        GROUP BY ti.tx_hash
        """,
        {"tx_hashes": tx_hashes, "network": network},
    )
    return {
        row[0]: {"resolved_input_value": int(row[1]), "unique_input_addresses": int(row[2])}
        for row in rows
    }


def get_address_activity(addresses: List[str], network: str) -> Dict[str, int]:
    """Return the total observed transaction count per address.

    Uses address_transactions (auto-populated by the materialized view on INSERT into transactions).
    Returns a dict {address: tx_count}. Missing keys = address not yet seen.
    """
    if not addresses:
        return {}
    rows = _get_client().execute(
        """
        SELECT address, count() AS tx_count
        FROM address_transactions
        WHERE network  = %(network)s
          AND address IN %(addresses)s
        GROUP BY address
        """,
        {"addresses": addresses, "network": network},
    )
    return {row[0]: int(row[1]) for row in rows}


def insert_analysis_results(results: List[Dict[str, Any]]):
    """Batch-insert analysis result rows into tx_analysis_results."""
    if not results:
        return
    _get_client().execute(
        """
        INSERT INTO tx_analysis_results (
            tx_hash, network, risk_score, risk_level, cluster_id,
            is_anomaly, anomaly_reasons, analysis_version, analyzed_at
        ) VALUES
        """,
        [
            (
                r["tx_hash"],
                r["network"],
                r["risk_score"],
                r["risk_level"],
                r["cluster_id"],
                r["is_anomaly"],
                r["anomaly_reasons"],
                r["analysis_version"],
                r["analyzed_at"],
            )
            for r in results
        ],
    )


def get_analysis_result(tx_hash: str) -> Optional[Dict[str, Any]]:
    """Return the latest analysis result for a single transaction."""
    rows = _get_client().execute(
        """
        SELECT tx_hash, network, risk_score, risk_level, cluster_id,
               is_anomaly, anomaly_reasons, analysis_version, analyzed_at
        FROM tx_analysis_results FINAL
        WHERE tx_hash = %(tx_hash)s
        LIMIT 1
        """,
        {"tx_hash": tx_hash},
    )
    if not rows:
        return None
    keys = ("tx_hash", "network", "risk_score", "risk_level", "cluster_id",
            "is_anomaly", "anomaly_reasons", "analysis_version", "analyzed_at")
    return dict(zip(keys, rows[0]))


def get_analysis_results(
    network: str,
    risk_level: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return analysis results, optionally filtered by risk_level."""
    if risk_level:
        rows = _get_client().execute(
            """
            SELECT tx_hash, network, risk_score, risk_level, cluster_id,
                   is_anomaly, anomaly_reasons, analysis_version, analyzed_at
            FROM tx_analysis_results FINAL
            WHERE network = %(network)s AND risk_level = %(risk_level)s
            ORDER BY analyzed_at DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"network": network, "risk_level": risk_level, "limit": limit, "offset": offset},
        )
    else:
        rows = _get_client().execute(
            """
            SELECT tx_hash, network, risk_score, risk_level, cluster_id,
                   is_anomaly, anomaly_reasons, analysis_version, analyzed_at
            FROM tx_analysis_results FINAL
            WHERE network = %(network)s
            ORDER BY analyzed_at DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"network": network, "limit": limit, "offset": offset},
        )
    keys = ("tx_hash", "network", "risk_score", "risk_level", "cluster_id",
            "is_anomaly", "anomaly_reasons", "analysis_version", "analyzed_at")
    return [dict(zip(keys, row)) for row in rows]


def get_analysis_stats(network: str) -> Dict[str, Any]:
    """Return aggregate analysis statistics for a network."""
    rows = _get_client().execute(
        """
        SELECT
            count()                                         AS total_analyzed,
            avg(risk_score)                                 AS avg_risk_score,
            countIf(risk_level = 'HIGH')                    AS high_risk_count,
            countIf(is_anomaly = 1)                         AS anomaly_count,
            uniq(cluster_id)                                AS cluster_count,
            max(analyzed_at)                                AS last_run_at
        FROM tx_analysis_results FINAL
        WHERE network = %(network)s
        """,
        {"network": network},
    )
    # ClickHouse aggregate queries always return exactly one row even on empty tables.
    # When the table is empty: count()=0, avg()=nan, max(DateTime)=epoch (1970-01-01).
    # Normalise these to the expected zero/None values before returning.
    keys = ("total_analyzed", "avg_risk_score", "high_risk_count",
            "anomaly_count", "cluster_count", "last_run_at")
    result = dict(zip(keys, rows[0])) if rows else {
        "total_analyzed": 0, "avg_risk_score": None,
        "high_risk_count": 0, "anomaly_count": 0,
        "cluster_count": 0, "last_run_at": None,
    }
    if result["total_analyzed"] == 0:
        result["avg_risk_score"] = None
        result["last_run_at"] = None
    return result


def _execute_query(query: str) -> list:
    """Execute a raw SELECT query. Called via execute_query_async (executor)."""
    return _get_client().execute(query)


async def execute_query_async(query: str) -> list:
    """Non-blocking wrapper: runs a raw SELECT query on the ClickHouse executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ch_executor, _execute_query, query)


async def get_analysis_result_async(tx_hash: str) -> Optional[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ch_executor, get_analysis_result, tx_hash)


async def get_analysis_results_async(
    network: str,
    risk_level: Optional[str],
    limit: int,
    offset: int,
) -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor, get_analysis_results, network, risk_level, limit, offset
    )


async def get_analysis_stats_async(network: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ch_executor, get_analysis_stats, network)
