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

        # Multi-class detection tables

        # Extended UTxO-level features, populated inline during ingestion.
        # One row per output per transaction.
        client.execute("""
            CREATE TABLE IF NOT EXISTS utxo_features (
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
            ) ENGINE = MergeTree()
            ORDER BY (network, tx_hash, output_index)
            PARTITION BY toYYYYMM(ingestion_timestamp)
        """)

        # Transaction-level script execution features, populated inline
        # during ingestion.  One row per transaction.
        client.execute("""
            CREATE TABLE IF NOT EXISTS tx_script_features (
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
            ) ENGINE = MergeTree()
            ORDER BY (network, tx_hash)
            PARTITION BY toYYYYMM(ingestion_timestamp)
        """)

        # Multi-class scoring output.  One row per scored transaction.
        # Each attack class gets an independent 0-100 score; -1 means the
        # gate condition failed (class not applicable).
        client.execute("""
            CREATE TABLE IF NOT EXISTS tx_class_scores (
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
                analysis_version String,
                analyzed_at      DateTime,
                INDEX idx_risk_band  risk_band TYPE bloom_filter GRANULARITY 1,
                INDEX idx_max_class  max_class TYPE bloom_filter GRANULARITY 1,
                INDEX idx_analyzed   analyzed_at TYPE minmax GRANULARITY 1
            ) ENGINE = ReplacingMergeTree(analyzed_at)
            ORDER BY (network, tx_hash)
            PARTITION BY toYYYYMMDD(analyzed_at)
        """)

        # Per-script / per-policy / global baseline statistics used by the
        # percentile normalisation framework.  Updated daily (or on bootstrap).
        client.execute("""
            CREATE TABLE IF NOT EXISTS baselines (
                scope_type   String,
                scope_id     String,
                feature      String,
                p50          Float64,
                p99          Float64,
                sample_count UInt64,
                computed_at  DateTime,
                window_days  UInt16
            ) ENGINE = ReplacingMergeTree(computed_at)
            ORDER BY (scope_type, scope_id, feature)
        """)

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

        # ------------------------------------------------------------------
        # Populate extended feature tables from raw_data (best-effort)
        try:
            from app.analysis.features import extract_utxo_features, extract_tx_script_features

            all_utxo_features = []
            all_script_features = []
            for tx in transactions:
                if not tx.raw_data:
                    continue
                net = tx.network or settings.CARDANO_NETWORK
                utxo_rows = extract_utxo_features(tx.tx_hash, net, tx.raw_data)
                all_utxo_features.extend(utxo_rows)
                script_row = extract_tx_script_features(tx.tx_hash, net, tx.raw_data)
                if script_row:
                    all_script_features.append(script_row)

            if all_utxo_features:
                insert_utxo_features(all_utxo_features)
            if all_script_features:
                insert_tx_script_features(all_script_features)
        except Exception as e:
            # Feature extraction is non-critical; log and continue
            logger.warning(f"Feature extraction failed (non-fatal): {e}")

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


def get_outputs_for_refs(
    refs: List[tuple],
    network: str,
) -> Dict[tuple, tuple]:
    """Batch-fetch output address and amount for a list of (tx_hash, output_index) pairs.

    Returns {(tx_hash, output_index): (address, amount)} for found outputs.
    Used at ingestion time to resolve input values from previously ingested blocks.
    """
    if not refs:
        return {}
    # Deduplicate and build a set for filtering
    ref_set = set(refs)
    unique_tx_hashes = list({r[0] for r in ref_set})
    rows = _get_client().execute(
        """
        SELECT tx_hash, output_index, address, amount
        FROM transaction_outputs
        WHERE tx_hash IN %(tx_hashes)s
          AND network = %(network)s
          AND is_collateral = 0
        """,
        {"tx_hashes": unique_tx_hashes, "network": network},
    )
    # Only return rows matching requested (tx_hash, output_index) pairs
    return {
        (r[0], r[1]): (r[2], int(r[3]))
        for r in rows if (r[0], r[1]) in ref_set
    }


async def get_outputs_for_refs_async(
    refs: List[tuple],
    network: str,
) -> Dict[tuple, tuple]:
    """Async wrapper for get_outputs_for_refs."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ch_executor, get_outputs_for_refs, refs, network)


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


def _execute_query(query: str, params: Optional[Dict] = None) -> list:
    """Execute a parameterized SELECT query. Called via execute_query_async."""
    return _get_client().execute(query, params or {})


async def execute_query_async(query: str, params: Optional[Dict] = None) -> list:
    """Non-blocking wrapper: runs a parameterized SELECT on the ClickHouse executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor, _execute_query, query, params,
    )



# ---------------------------------------------------------------------------
# Multi-class scoring, feature tables, baselines

def insert_utxo_features(rows: List[tuple]):
    """Batch-insert UTxO-level feature rows extracted during ingestion."""
    if not rows:
        return
    _get_client().execute(
        """
        INSERT INTO utxo_features (
            tx_hash, network, output_index, address, is_script_address,
            ada_amount, value_cbor_bytes, unique_policy_count, unique_token_count,
            datum_present, datum_bytes, datum_ratio, utxo_total_bytes
        ) VALUES
        """,
        rows,
    )


def insert_tx_script_features(rows: List[tuple]):
    """Batch-insert transaction-level script feature rows."""
    if not rows:
        return
    _get_client().execute(
        """
        INSERT INTO tx_script_features (
            tx_hash, network, redeemers_count, spending_inputs,
            exunits_mem_total, exunits_cpu_total, mint_policy_count, mint_entries
        ) VALUES
        """,
        rows,
    )


def insert_class_scores(results: List[Dict[str, Any]]):
    """Batch-insert multi-class scoring results into tx_class_scores."""
    if not results:
        return
    _get_client().execute(
        """
        INSERT INTO tx_class_scores (
            tx_hash, network,
            token_dust, large_value, large_datum, multiple_sat,
            front_running, sandwich, circular, fake_token, phishing,
            max_score, max_class, risk_band, sub_scores,
            analysis_version, analyzed_at
        ) VALUES
        """,
        [
            (
                r["tx_hash"], r["network"],
                r.get("token_dust", -1), r.get("large_value", -1),
                r.get("large_datum", -1), r.get("multiple_sat", -1),
                r.get("front_running", -1), r.get("sandwich", -1),
                r.get("circular", -1), r.get("fake_token", -1),
                r.get("phishing", -1),
                r["max_score"], r["max_class"], r["risk_band"],
                json.dumps(r.get("sub_scores", {})),
                r["analysis_version"], r["analyzed_at"],
            )
            for r in results
        ],
    )


def get_class_scores(tx_hash: str) -> Optional[Dict[str, Any]]:
    """Return the latest multi-class score vector for a single transaction."""
    rows = _get_client().execute(
        """
        SELECT tx_hash, network,
               token_dust, large_value, large_datum, multiple_sat,
               front_running, sandwich, circular, fake_token, phishing,
               max_score, max_class, risk_band, sub_scores,
               analysis_version, analyzed_at
        FROM tx_class_scores FINAL
        WHERE tx_hash = %(tx_hash)s
        LIMIT 1
        """,
        {"tx_hash": tx_hash},
    )
    if not rows:
        return None
    keys = (
        "tx_hash", "network",
        "token_dust", "large_value", "large_datum", "multiple_sat",
        "front_running", "sandwich", "circular", "fake_token", "phishing",
        "max_score", "max_class", "risk_band", "sub_scores",
        "analysis_version", "analyzed_at",
    )
    result = dict(zip(keys, rows[0]))
    if isinstance(result["sub_scores"], str):
        try:
            result["sub_scores"] = json.loads(result["sub_scores"])
        except (json.JSONDecodeError, TypeError):
            result["sub_scores"] = {}
    return result


# ---------------------------------------------------------------------------
# Baseline read/write
# ---------------------------------------------------------------------------

def get_baseline(scope_type: str, scope_id: str, feature: str) -> Optional[Dict[str, Any]]:
    """Return the latest baseline for a given (scope_type, scope_id, feature)."""
    rows = _get_client().execute(
        """
        SELECT p50, p99, sample_count, computed_at, window_days
        FROM baselines FINAL
        WHERE scope_type = %(scope_type)s
          AND scope_id   = %(scope_id)s
          AND feature    = %(feature)s
        LIMIT 1
        """,
        {"scope_type": scope_type, "scope_id": scope_id, "feature": feature},
    )
    if not rows:
        return None
    keys = ("p50", "p99", "sample_count", "computed_at", "window_days")
    return dict(zip(keys, rows[0]))


def insert_baselines(rows: List[tuple]):
    """Batch-insert or update baseline statistics."""
    if not rows:
        return
    _get_client().execute(
        """
        INSERT INTO baselines (
            scope_type, scope_id, feature, p50, p99,
            sample_count, computed_at, window_days
        ) VALUES
        """,
        rows,
    )


def get_class_scores_list(
    network: str,
    risk_band: Optional[str] = None,
    attack_class: Optional[str] = None,
    min_score: float = 0.0,
    sort: str = "score",
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return multi-class score rows with optional filters.

    sort: "score" (default) or "date" (most recent first).
    """
    _CLASS_COLS = (
        "token_dust", "large_value", "large_datum", "multiple_sat",
        "front_running", "sandwich", "circular", "fake_token", "phishing",
    )
    _ALLOWED_SORTS = {
        "score": "max_score DESC, analyzed_at DESC",
        "date": "analyzed_at DESC, max_score DESC",
    }
    order_clause = _ALLOWED_SORTS.get(sort, _ALLOWED_SORTS["score"])
    if attack_class and attack_class not in _CLASS_COLS:
        raise ValueError(f"Invalid attack_class '{attack_class}'")

    conditions = ["network = %(network)s"]
    params: Dict[str, Any] = {
        "network": network, "limit": limit, "offset": offset,
    }
    if risk_band:
        conditions.append("risk_band = %(risk_band)s")
        params["risk_band"] = risk_band
    if attack_class and attack_class in _CLASS_COLS:
        # Safe: attack_class validated against _CLASS_COLS allowlist above
        conditions.append(f"`{attack_class}` >= %(min_score)s")
        params["min_score"] = min_score
    elif min_score > 0:
        conditions.append("max_score >= %(min_score)s")
        params["min_score"] = min_score

    where = " AND ".join(conditions)
    # Query scores first, then batch-fetch tx details separately.
    # ClickHouse 26+ does not allow FINAL on tables inside JOINs.
    rows = _get_client().execute(
        f"""
        SELECT tx_hash, network,
               token_dust, large_value, large_datum, multiple_sat,
               front_running, sandwich, circular, fake_token, phishing,
               max_score, max_class, risk_band, sub_scores,
               analysis_version, analyzed_at
        FROM tx_class_scores FINAL
        WHERE {where}
        ORDER BY {order_clause}
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    score_keys = (
        "tx_hash", "network",
        *_CLASS_COLS,
        "max_score", "max_class", "risk_band", "sub_scores",
        "analysis_version", "analyzed_at",
    )
    # Batch-fetch fee/output_count for matched tx_hashes
    tx_hashes = [r[0] for r in rows]
    tx_details: Dict[str, Dict[str, Any]] = {}
    if tx_hashes:
        detail_rows = _get_client().execute(
            """
            SELECT tx_hash, fee, output_count
            FROM transactions
            WHERE tx_hash IN %(hashes)s AND network = %(network)s
            """,
            {"hashes": tx_hashes, "network": network},
        )
        for dr in detail_rows:
            tx_details[dr[0]] = {"fee": dr[1], "output_count": dr[2]}
    keys = (*score_keys, "fee", "output_count")
    results = []
    for row in rows:
        d = dict(zip(score_keys, row))
        detail = tx_details.get(d["tx_hash"], {})
        d["fee"] = detail.get("fee")
        d["output_count"] = detail.get("output_count")
        if isinstance(d["sub_scores"], str):
            try:
                d["sub_scores"] = json.loads(d["sub_scores"])
            except (json.JSONDecodeError, TypeError):
                d["sub_scores"] = {}
        results.append(d)
    return results


async def get_class_scores_list_async(
    network: str, risk_band: Optional[str], attack_class: Optional[str],
    min_score: float, sort: str = "score", limit: int = 100, offset: int = 0,
) -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor, get_class_scores_list,
        network, risk_band, attack_class, min_score, sort, limit, offset,
    )


async def get_class_scores_async(tx_hash: str) -> Optional[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ch_executor, get_class_scores, tx_hash)


def get_class_scores_stats(network: str) -> Dict[str, Any]:
    """Per-class distribution stats for a network."""
    _CLASS_COLS = (
        "token_dust", "large_value", "large_datum", "multiple_sat",
        "front_running", "sandwich", "circular", "fake_token", "phishing",
    )
    # Build per-class aggregation: count of scored (>= 0), avg, max
    agg_parts = []
    for col in _CLASS_COLS:
        agg_parts.append(
            f"countIf({col} >= 0) AS {col}_count, "
            f"avgIf({col}, {col} >= 0) AS {col}_avg, "
            f"maxIf({col}, {col} >= 0) AS {col}_max"
        )
    agg_sql = ", ".join(agg_parts)
    rows = _get_client().execute(
        f"""
        SELECT count() AS total,
               countIf(risk_band = 'Critical') AS critical_count,
               countIf(risk_band = 'High') AS high_count,
               countIf(risk_band = 'Moderate') AS moderate_count,
               countIf(risk_band = 'Low') AS low_count,
               avg(max_score) AS avg_max_score,
               max(analyzed_at) AS last_analyzed_at,
               {agg_sql}
        FROM tx_class_scores FINAL
        WHERE network = %(network)s
        """,
        {"network": network},
    )
    if not rows:
        return {}
    row = rows[0]
    idx = 0
    result: Dict[str, Any] = {
        "total": row[idx], "critical_count": row[idx+1],
        "high_count": row[idx+2], "moderate_count": row[idx+3],
        "low_count": row[idx+4], "avg_max_score": row[idx+5],
        "last_analyzed_at": row[idx+6],
    }
    idx = 7
    per_class = {}
    for col in _CLASS_COLS:
        per_class[col] = {
            "scored_count": row[idx], "avg_score": row[idx+1], "max_score": row[idx+2],
        }
        idx += 3
    result["per_class"] = per_class
    return result


async def get_class_scores_stats_async(network: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ch_executor, get_class_scores_stats, network)


def get_baselines_for_scope(
    scope_type: str, scope_id: str,
) -> List[Dict[str, Any]]:
    """Return all baselines for a given scope."""
    rows = _get_client().execute(
        """
        SELECT feature, p50, p99, sample_count, computed_at, window_days
        FROM baselines FINAL
        WHERE scope_type = %(scope_type)s AND scope_id = %(scope_id)s
        ORDER BY feature
        """,
        {"scope_type": scope_type, "scope_id": scope_id},
    )
    keys = ("feature", "p50", "p99", "sample_count", "computed_at", "window_days")
    return [dict(zip(keys, r)) for r in rows]


async def get_baselines_for_scope_async(
    scope_type: str, scope_id: str,
) -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor, get_baselines_for_scope, scope_type, scope_id,
    )


def get_unanalyzed_transactions(network: str, batch_size: int) -> List[Dict[str, Any]]:
    """Return transactions that have no multi-class score yet.

    Fetches raw_data alongside the standard fields so that the feature
    extraction pipeline can derive UTxO-level and script-level features
    without a second round-trip.
    """
    rows = _get_client().execute(
        """
        SELECT t.tx_hash, t.network, t.fee, t.input_count, t.output_count,
               t.total_output_value, t.metadata, t.addresses, t.raw_data,
               t.slot, t.block_height, t.timestamp
        FROM transactions t
        LEFT ANTI JOIN tx_class_scores s
          ON t.tx_hash = s.tx_hash AND t.network = s.network
        WHERE t.network = %(network)s
        ORDER BY t.ingestion_timestamp ASC
        LIMIT %(batch_size)s
        """,
        {"network": network, "batch_size": batch_size},
    )
    keys = ("tx_hash", "network", "fee", "input_count", "output_count",
            "total_output_value", "metadata", "addresses", "raw_data",
            "slot", "block_height", "timestamp")
    return [dict(zip(keys, row)) for row in rows]
