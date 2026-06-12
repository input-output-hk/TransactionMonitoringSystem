"""ClickHouse database connection and operations"""

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple
from clickhouse_driver import Client
from clickhouse_driver.errors import Error as ClickHouseError

from app.config import settings
from app.db import clickhouse_schema
from app.db.clickhouse_schema import (  # noqa: F401  (re-exported API)
    DEDUP_TABLE_KEYS,
    SCHEMA_DDL,
)
from app.models.transaction import NormalizedTransaction

logger = logging.getLogger(__name__)

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


async def _in_executor(fn, *args):
    """Run a blocking ClickHouse call on the dedicated executor.

    The single home for the ``run_in_executor(_ch_executor, ...)`` idiom
    every ``*_async`` wrapper repeats; keyword-rich callables pass a
    ``functools.partial``.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ch_executor, fn, *args)


def _get_client() -> Client:
    """Return the per-thread ClickHouse Client, creating it on first call.

    When a prior ``client.execute()`` hits a network error, clickhouse_driver's
    internal error handling calls ``disconnect()`` on the connection, which
    sets ``connection.connected = False`` but leaves the cached Client object
    in place. A naive reuse then blocks or fails on the next call. Check the
    connection state here and tear down a dead client so the next request
    gets a freshly opened socket.
    """
    client = getattr(_thread_local, "client", None)
    if client is not None:
        conn = getattr(client, "connection", None)
        if conn is not None and not getattr(conn, "connected", True):
            try:
                client.disconnect()
            except Exception:
                pass
            _thread_local.client = None
            client = None
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


# ---------------------------------------------------------------------------
# Schema. DDL templates, table creation, the legacy-layout guard, and
# retention TTLs live in app.db.clickhouse_schema (pure functions of a
# connected Client). SCHEMA_DDL / DEDUP_TABLE_KEYS are re-exported here so
# existing imports (e.g. scripts/migrate_dedup_schema.py reads
# clickhouse.SCHEMA_DDL) keep working.


def execute_schema():
    """Create ClickHouse tables if they don't exist"""
    client = _get_client()
    try:
        clickhouse_schema.create_all(client)
        logger.info("ClickHouse schema initialized")
    except ClickHouseError as e:
        logger.error(f"Failed to create ClickHouse schema: {e}")
        raise


def _serialize_raw_data(raw_data: Optional[Dict[str, Any]]) -> Tuple[str, int]:
    """Serialize a tx's raw Ogmios payload for the transactions INSERT.

    Returns ``(json_string, truncated_flag)``. When RAW_DATA_MAX_BYTES > 0
    and the serialized payload exceeds it, an EMPTY string is stored with the
    flag set — never a sliced prefix, which is invalid JSON and used to make
    the analysis engine silently score the tx with every gate closed. The
    full payload stays available in the raw store (ADR-009); the engine falls
    back to it when the flag is set.
    """
    if not raw_data:
        return "", 0
    raw_json = json.dumps(raw_data)
    max_bytes = settings.RAW_DATA_MAX_BYTES
    # json.dumps defaults to ensure_ascii=True, so len() == byte length.
    if max_bytes > 0 and len(raw_json) > max_bytes:
        return "", 1
    return raw_json, 0


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
        tx_rows = []
        for tx in transactions:
            raw_json, raw_truncated = _serialize_raw_data(tx.raw_data)
            tx_rows.append((
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
                raw_json,
                raw_truncated,
                1 if tx.script_valid else 0,
                tx.ingestion_timestamp or now,
            ))
        client.execute(
            """
            INSERT INTO transactions (
                tx_hash, network, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
                input_count, output_count, total_input_value, total_output_value,
                addresses, metadata, raw_data, raw_data_truncated, script_valid, ingestion_timestamp
            ) VALUES
            """,
            tx_rows,
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
                1 if inp.is_unspent_attempt else 0,
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
                    address, amount, assets, is_reference, is_collateral,
                    is_unspent_attempt, ingestion_timestamp
                ) VALUES
                """,
                all_inputs,
            )

        all_outputs = [
            (
                tx.tx_hash,
                tx.network or settings.CARDANO_NETWORK,
                # Explicit on-chain index wins (collateral returns sit at
                # the regular-output count, not their list position).
                out.output_index if out.output_index is not None else idx,
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
    await _in_executor(insert_transactions_batch, transactions)


# ---------------------------------------------------------------------------
# Analysis Engine helpers
# ---------------------------------------------------------------------------

# Tables holding per-transaction chain facts that must be purged when the
# chain rolls back past their slot. archived_alerts is deliberately absent:
# it is admin curation, not chain state.
#
# Order is load-bearing for idempotency: transactions is the table the orphan
# hashes are SELECTed from, so it must be deleted LAST. If it were deleted
# earlier and a later table's DELETE failed, the retry would re-select from
# transactions, find nothing, and leave the remaining tables holding orphans
# forever (a surviving stale tx_class_scores row then permanently blocks
# re-scoring via the unanalyzed anti-join). tx_class_scores stays as late as
# possible (second-to-last) to minimize the window where an in-flight engine
# batch re-inserts a score row after its purge.
_ROLLBACK_CLEANUP_TABLES: Tuple[str, ...] = (
    "transaction_inputs",
    "transaction_outputs",
    "utxo_features",
    "tx_script_features",
    "address_transactions",
    "tx_class_scores",
    "transactions",
)

# ClickHouse >= 24.7 refuses lightweight DELETE on a table with projections
# unless told how to handle them ('throw' is the default), which would turn
# every rollback purge on the projected transactions table into a permanent
# chain-sync crash loop. 'rebuild' keeps the list-endpoint projection correct
# on surviving rows; 'drop' would silently degrade reads on mutated parts.
# On 26.x the gate reads the TABLE-level merge-tree setting (declared in
# clickhouse_schema's transactions DDL/migration; verified live on 26.1.3);
# this per-query copy covers 24.7-25.x servers where the gate reads the
# query setting. Harmless on the cleanup tables that have no projections.
_LIGHTWEIGHT_DELETE_SETTINGS = {"lightweight_mutation_projection_mode": "rebuild"}


def delete_rolled_back_txs(network: str, rollback_slot: int) -> List[str]:
    """Delete all rows for transactions confirmed after ``rollback_slot``.

    Called on a ChainSync rollBackward: blocks past the rollback point are
    off-chain, so their rows would otherwise feed scorers, baselines, and
    API reads forever. Uses lightweight DELETEs (ClickHouse 22.8+). If the
    transaction later re-confirms on the new fork, ChainSync re-delivers it
    and the ReplacingMergeTree insert is a clean upsert with the new block
    coordinates. Returns the orphaned tx hashes (callers use len() for the
    count; the rollback handler feeds them to the delayed score repurge).

    Idempotent under partial failure: the orphan hashes are selected from
    transactions, and transactions is deleted LAST (see
    _ROLLBACK_CLEANUP_TABLES). If any earlier table's DELETE fails, the
    hash source is still intact, so a retry re-selects the same hashes and
    deletes whatever remains. tx_class_scores is second-to-last, which
    minimizes (but cannot close) the window where an engine batch in
    flight re-inserts a score row after the purge; delete_score_rows runs
    again after a delay for that race.
    """
    client = _get_client()
    rows = client.execute(
        """
        SELECT DISTINCT tx_hash FROM transactions FINAL
        WHERE network = %(network)s AND slot > %(slot)s
        """,
        {"network": network, "slot": rollback_slot},
    )
    hashes = [r[0] for r in rows]
    if not hashes:
        return []
    for table in _ROLLBACK_CLEANUP_TABLES:
        client.execute(
            f"DELETE FROM {table} WHERE network = %(network)s AND tx_hash IN %(hashes)s",
            {"network": network, "hashes": hashes},
            settings=_LIGHTWEIGHT_DELETE_SETTINGS,
        )
    return hashes


async def delete_rolled_back_txs_async(network: str, rollback_slot: int) -> List[str]:
    """Async wrapper for delete_rolled_back_txs (runs on the CH executor)."""
    return await _in_executor(delete_rolled_back_txs, network, rollback_slot)


def delete_score_rows(network: str, hashes: List[str]) -> None:
    """Targeted tx_class_scores purge: the delayed second rollback pass.

    Closes the purge/score-writer race: an engine batch holding rolled-back
    rows when the purge ran inserts its scores AFTER the first DELETE, and
    the stale row then blocks re-scoring forever via the anti-join.
    """
    if not hashes:
        return
    _get_client().execute(
        "DELETE FROM tx_class_scores "
        "WHERE network = %(network)s AND tx_hash IN %(hashes)s",
        {"network": network, "hashes": hashes},
        settings=_LIGHTWEIGHT_DELETE_SETTINGS,
    )


async def delete_score_rows_async(network: str, hashes: List[str]) -> None:
    """Async wrapper for delete_score_rows (runs on the CH executor)."""
    await _in_executor(delete_score_rows, network, hashes)


def get_input_resolution(tx_hashes: List[str], network: str) -> Dict[str, Dict[str, Any]]:
    """Resolve input values and unique source addresses for a batch of transactions.

    Joins transaction_inputs against transaction_outputs on (input_tx_hash, input_index_in_tx).
    Only consumed inputs are considered (non-collateral, non-reference,
    non-attempted: a failed tx's regular inputs were never spent).
    Returns a dict keyed by tx_hash. Missing keys = no resolvable inputs (outputs pre-date sync).
    """
    if not tx_hashes:
        return {}
    # FINAL-in-subquery on BOTH join sides: this is a sum over a join, so a
    # not-yet-merged ReplacingMergeTree duplicate on either side would double
    # the resolved input value feeding the scorers. (FINAL directly on a
    # joined table is rejected by ClickHouse; subqueries are the supported
    # form.) The inner ref-bound on to2 keeps the FINAL scan proportional to
    # the batch, not the table.
    # Output side has NO is_collateral filter: only failed txs persist
    # collateral-return output rows, and those ARE real spendable UTxOs;
    # excluding them made any tx spending one unresolvable (review finding).
    rows = _get_client().execute(
        """
        SELECT
            ti.tx_hash,
            sum(coalesce(to2.amount, 0))  AS resolved_input_value,
            uniqExact(to2.address)        AS unique_input_addresses
        FROM (
            SELECT tx_hash, network, input_tx_hash, input_index_in_tx
            FROM transaction_inputs FINAL
            WHERE tx_hash      IN %(tx_hashes)s
              AND network       = %(network)s
              AND is_collateral = 0
              AND is_reference  = 0
              AND is_unspent_attempt = 0
        ) ti
        LEFT JOIN (
            SELECT tx_hash, network, output_index, address, amount
            FROM transaction_outputs FINAL
            WHERE network = %(network)s
              AND tx_hash IN (
                  SELECT input_tx_hash
                  FROM transaction_inputs FINAL
                  WHERE tx_hash      IN %(tx_hashes)s
                    AND network       = %(network)s
                    AND is_collateral = 0
                    AND is_reference  = 0
                    AND is_unspent_attempt = 0
              )
        ) to2
            ON  ti.input_tx_hash     = to2.tx_hash
            AND ti.input_index_in_tx = to2.output_index
            AND ti.network           = to2.network
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
    # FINAL: resolved amounts feed total_input_value at ingestion; a
    # pre-merge duplicate row is harmless for the dict shape (same key,
    # same value) but FINAL keeps the contract exact.
    rows = _get_client().execute(
        """
        SELECT tx_hash, output_index, address, amount
        FROM transaction_outputs FINAL
        WHERE tx_hash IN %(tx_hashes)s
          AND network = %(network)s
        """,
        {"tx_hashes": unique_tx_hashes, "network": network},
    )
    # No is_collateral filter: only failed txs persist collateral-return
    # output rows and those are real spendable UTxOs (Babbage); excluding
    # them left total_input_value NULL on any tx spending one.
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
    return await _in_executor(get_outputs_for_refs, refs, network)


def get_address_activity(addresses: List[str], network: str) -> Dict[str, int]:
    """Return the total observed transaction count per address.

    Uses address_transactions (auto-populated by the materialized view on INSERT into transactions).
    Returns a dict {address: tx_count}. Missing keys = address not yet seen.
    """
    if not addresses:
        return {}
    # uniqExact(tx_hash) rather than count(): the MV fires on every INSERT
    # into transactions, so a replayed block produces duplicate rows until
    # the ReplacingMergeTree merge runs; counting distinct tx hashes is
    # correct in both states and cheaper than FINAL here.
    rows = _get_client().execute(
        """
        SELECT address, uniqExact(tx_hash) AS tx_count
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
    return await _in_executor(_execute_query, query, params)



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


# ---------------------------------------------------------------------------
# Baseline read/write
# ---------------------------------------------------------------------------

# In-process TTL cache for baseline lookups. Baselines change once per
# recompute (daily cadence) but were fetched with a point SELECT ... FINAL
# per feature per scored transaction: the dominant N+1 in the engine's
# per-tx query budget. Negative results are cached too (most scripts have
# no per-script baseline, so misses dominate). insert_baselines() clears
# the cache, so the daily recompute invalidates atomically; the TTL is the
# backstop for out-of-band writes. Guarded by a lock: callers run on the
# ClickHouse executor threads.
_baseline_cache: Dict[tuple, tuple] = {}
_baseline_cache_lock = threading.Lock()


def _baseline_cache_clear() -> None:
    with _baseline_cache_lock:
        _baseline_cache.clear()


def get_baseline(
    network: str, scope_type: str, scope_id: str, feature: str,
) -> Optional[Dict[str, Any]]:
    """Return the latest baseline for a given (network, scope_type, scope_id, feature)."""
    ttl = settings.BASELINE_CACHE_TTL_SECONDS
    cache_key = (network, scope_type, scope_id, feature)
    if ttl > 0:
        with _baseline_cache_lock:
            hit = _baseline_cache.get(cache_key)
            if hit is not None:
                value, fetched_at = hit
                if time.monotonic() - fetched_at < ttl:
                    return dict(value) if value is not None else None
                _baseline_cache.pop(cache_key, None)
    rows = _get_client().execute(
        """
        SELECT p50, p99, sample_count, computed_at, window_days
        FROM baselines FINAL
        WHERE network    = %(network)s
          AND scope_type = %(scope_type)s
          AND scope_id   = %(scope_id)s
          AND feature    = %(feature)s
        LIMIT 1
        """,
        {
            "network": network,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "feature": feature,
        },
    )
    keys = ("p50", "p99", "sample_count", "computed_at", "window_days")
    result = dict(zip(keys, rows[0])) if rows else None
    if ttl > 0:
        with _baseline_cache_lock:
            if len(_baseline_cache) >= settings.BASELINE_CACHE_MAX_ENTRIES:
                # Blunt overflow policy: drop everything and let the hot
                # keys refill. Simpler than LRU bookkeeping and overflow is
                # effectively unreachable at the configured size.
                _baseline_cache.clear()
            _baseline_cache[cache_key] = (
                dict(result) if result is not None else None,
                time.monotonic(),
            )
    return result


def insert_baselines(rows: List[tuple]):
    """Batch-insert or update baseline statistics.

    Each row tuple: ``(network, scope_type, scope_id, feature, p50, p99,
    sample_count, computed_at, window_days)``.
    """
    if not rows:
        return
    _get_client().execute(
        """
        INSERT INTO baselines (
            network, scope_type, scope_id, feature, p50, p99,
            sample_count, computed_at, window_days
        ) VALUES
        """,
        rows,
    )
    # New baselines invalidate every cached lookup (recompute writes all
    # scopes in a handful of batches; a full clear is the simple, correct
    # granularity).
    _baseline_cache_clear()


def insert_baseline_drift_event(
    network: str,
    scope_type: str,
    scope_id: str,
    feature: str,
    old_p99: float,
    new_p99: float,
    drift_ratio: float,
    detected_at: datetime,
    axis: str = "p99",
    applied: bool = False,
):
    """Record a baseline drift event (held or applied).

    ``axis`` names the drifting percentile; the legacy ``old_p99``/
    ``new_p99`` column names are kept but hold that axis's old/new values.
    ``applied`` is True for recall-safe drifts that were inserted anyway.
    """
    _get_client().execute(
        """
        INSERT INTO baseline_drift_events (
            network, scope_type, scope_id, feature,
            old_p99, new_p99, drift_ratio, detected_at, axis, applied
        ) VALUES
        """,
        [(
            network, scope_type, scope_id, feature,
            float(old_p99), float(new_p99), float(drift_ratio), detected_at,
            axis, 1 if applied else 0,
        )],
    )


# Baseline feature name -> the multiple_sat evidence JSON key that carries its
# per-tx value. Only the VALUE-extraction axis is per-script-calibrated (see
# baselines._MULTIPLE_SAT_PER_SCRIPT_FEATURES for why exunits/n_inputs are
# excluded). These are computed only at scoring time (they need resolved
# inputs), so they are not in any ingestion feature table; their values are read
# back out of the persisted ``tx_class_scores.evidence``. Keys are a fixed
# allowlist (no user input) so they are safe to interpolate.
def get_baselines_for_scope(
    network: str, scope_type: str, scope_id: str,
) -> List[Dict[str, Any]]:
    """Return all baselines for a given scope on a given network."""
    rows = _get_client().execute(
        """
        SELECT feature, p50, p99, sample_count, computed_at, window_days
        FROM baselines FINAL
        WHERE network = %(network)s
          AND scope_type = %(scope_type)s
          AND scope_id = %(scope_id)s
        ORDER BY feature
        """,
        {"network": network, "scope_type": scope_type, "scope_id": scope_id},
    )
    keys = ("feature", "p50", "p99", "sample_count", "computed_at", "window_days")
    return [dict(zip(keys, r)) for r in rows]


async def get_baselines_for_scope_async(
    network: str, scope_type: str, scope_id: str,
) -> List[Dict[str, Any]]:
    return await _in_executor(get_baselines_for_scope, network, scope_type, scope_id)

# ---------------------------------------------------------------------------
# tx_class_scores layer: lives in app.db.clickhouse_scores; re-exported here
# so every existing caller and test import path (clickhouse.insert_class_scores,
# clickhouse.get_unanalyzed_transactions, the _score_filter_conditions /
# _MULTIPLE_SAT_EVIDENCE_KEYS internals used by tests and baselines.py)
# keeps working unchanged. clickhouse_scores resolves the client/executor
# through this module at call time, so monkeypatching _get_client /
# _ch_executor here still reaches the moved code.
from app.db.clickhouse_scores import (  # noqa: E402, F401  (re-exported API)
    _CLASS_COLS,
    _MULTIPLE_SAT_EVIDENCE_KEYS,
    _score_filter_conditions,
    count_class_scores,
    count_class_scores_async,
    get_alert_timeseries,
    get_alert_timeseries_async,
    get_class_scores,
    get_class_scores_async,
    get_class_scores_list,
    get_class_scores_list_async,
    get_class_scores_stats,
    get_class_scores_stats_async,
    get_pending_count,
    get_unanalyzed_transactions,
    insert_class_scores,
    query_multiple_sat_extraction_percentiles,
)
