"""ClickHouse database connection and operations"""

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
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
                tx.ingestion_timestamp or now,
            ))
        client.execute(
            """
            INSERT INTO transactions (
                tx_hash, network, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
                input_count, output_count, total_input_value, total_output_value,
                addresses, metadata, raw_data, raw_data_truncated, ingestion_timestamp
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
    await _in_executor(insert_transactions_batch, transactions)


# ---------------------------------------------------------------------------
# Analysis Engine helpers
# ---------------------------------------------------------------------------

# Tables holding per-transaction chain facts that must be purged when the
# chain rolls back past their slot. archived_alerts is deliberately absent:
# it is admin curation, not chain state.
_ROLLBACK_CLEANUP_TABLES: Tuple[str, ...] = (
    "transactions",
    "transaction_inputs",
    "transaction_outputs",
    "utxo_features",
    "tx_script_features",
    "address_transactions",
    "tx_class_scores",
)


def delete_rolled_back_txs(network: str, rollback_slot: int) -> int:
    """Delete all rows for transactions confirmed after ``rollback_slot``.

    Called on a ChainSync rollBackward: blocks past the rollback point are
    off-chain, so their rows would otherwise feed scorers, baselines, and
    API reads forever. Uses lightweight DELETEs (ClickHouse 22.8+). If the
    transaction later re-confirms on the new fork, ChainSync re-delivers it
    and the ReplacingMergeTree insert is a clean upsert with the new block
    coordinates. Returns the number of orphaned tx hashes.

    Idempotent: re-running after a partial failure deletes whatever remains.
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
        return 0
    for table in _ROLLBACK_CLEANUP_TABLES:
        client.execute(
            f"DELETE FROM {table} WHERE network = %(network)s AND tx_hash IN %(hashes)s",
            {"network": network, "hashes": hashes},
        )
    return len(hashes)


async def delete_rolled_back_txs_async(network: str, rollback_slot: int) -> int:
    """Async wrapper for delete_rolled_back_txs (runs on the CH executor)."""
    return await _in_executor(delete_rolled_back_txs, network, rollback_slot)


def get_input_resolution(tx_hashes: List[str], network: str) -> Dict[str, Dict[str, Any]]:
    """Resolve input values and unique source addresses for a batch of transactions.

    Joins transaction_inputs against transaction_outputs on (input_tx_hash, input_index_in_tx).
    Only non-collateral, non-reference inputs are considered.
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
        ) ti
        LEFT JOIN (
            SELECT tx_hash, network, output_index, address, amount
            FROM transaction_outputs FINAL
            WHERE network = %(network)s
              AND is_collateral = 0
              AND tx_hash IN (
                  SELECT input_tx_hash
                  FROM transaction_inputs FINAL
                  WHERE tx_hash      IN %(tx_hashes)s
                    AND network       = %(network)s
                    AND is_collateral = 0
                    AND is_reference  = 0
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
            max_score, max_class, risk_band, sub_scores, evidence,
            corroboration_count, corroborating_classes,
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
                json.dumps(r.get("evidence", {}), default=str),
                r.get("corroboration_count", 0), r.get("corroborating_classes", ""),
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
               max_score, max_class, risk_band, sub_scores, evidence,
               corroboration_count, corroborating_classes,
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
        "max_score", "max_class", "risk_band", "sub_scores", "evidence",
        "corroboration_count", "corroborating_classes",
        "analysis_version", "analyzed_at",
    )
    result = dict(zip(keys, rows[0]))
    for json_key in ("sub_scores", "evidence"):
        if isinstance(result.get(json_key), str):
            try:
                result[json_key] = json.loads(result[json_key])
            except (json.JSONDecodeError, TypeError):
                result[json_key] = {}
    return result


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
):
    """Record a HELD baseline update (drift beyond threshold; not applied)."""
    _get_client().execute(
        """
        INSERT INTO baseline_drift_events (
            network, scope_type, scope_id, feature,
            old_p99, new_p99, drift_ratio, detected_at
        ) VALUES
        """,
        [(
            network, scope_type, scope_id, feature,
            float(old_p99), float(new_p99), float(drift_ratio), detected_at,
        )],
    )


# Baseline feature name -> the multiple_sat evidence JSON key that carries its
# per-tx value. Only the VALUE-extraction axis is per-script-calibrated (see
# baselines._MULTIPLE_SAT_PER_SCRIPT_FEATURES for why exunits/n_inputs are
# excluded). These are computed only at scoring time (they need resolved
# inputs), so they are not in any ingestion feature table; their values are read
# back out of the persisted ``tx_class_scores.evidence``. Keys are a fixed
# allowlist (no user input) so they are safe to interpolate.
_MULTIPLE_SAT_EVIDENCE_KEYS = (
    ("net_value_out_of_script", "value_extracted_lovelace"),
    ("n_assets_out_of_script", "n_assets_extracted"),
)


def query_multiple_sat_extraction_percentiles(
    network: str, window_days: int, min_samples: int,
) -> List[Dict[str, Any]]:
    """Per-script p50/p99 of the multiple_sat extraction features.

    Aggregates the already-persisted ``tx_class_scores.evidence`` over scored
    (``multiple_sat >= 0``) rows, grouped by the evidence's
    ``target_script_address``, within the trailing ``window_days``. Only scripts
    with at least ``min_samples`` scored spends are returned.

    Returns one dict per qualifying script::

        {"script": str, "sample_count": int,
         "<feature>": (p50, p99), ...}   # one entry per _MULTIPLE_SAT_EVIDENCE_KEYS

    ``quantileExact`` is used for determinism (idempotent recomputes). It holds
    each per-script group's values in memory; the 90-day window + daily-batch
    cadence keep that bounded. If a single hot mainnet script ever makes this a
    memory concern, switch to a deterministic approximate quantile (preserving
    idempotency) rather than a tighter window.
    """
    # Build the per-feature percentile projections from the fixed key allowlist.
    select_parts = []
    for feature, key in _MULTIPLE_SAT_EVIDENCE_KEYS:
        col = f"JSONExtractInt(evidence, 'multiple_sat', '{key}')"
        select_parts.append(f"quantileExact(0.50)(toFloat64({col})) AS {feature}_p50")
        select_parts.append(f"quantileExact(0.99)(toFloat64({col})) AS {feature}_p99")
    projections = ",\n                ".join(select_parts)

    rows = _get_client().execute(
        f"""
        SELECT
            JSONExtractString(evidence, 'multiple_sat', 'target_script_address') AS script,
            count() AS cnt,
            {projections}
        FROM tx_class_scores FINAL
        WHERE network = %(network)s
          AND multiple_sat >= 0
          AND analyzed_at >= now() - INTERVAL %(days)s DAY
          AND JSONExtractString(evidence, 'multiple_sat', 'target_script_address') != ''
        GROUP BY script
        HAVING cnt >= %(min_samples)s
        """,
        {"network": network, "days": window_days, "min_samples": min_samples},
    )

    results: List[Dict[str, Any]] = []
    for row in rows:
        script, cnt = row[0], int(row[1])
        rec: Dict[str, Any] = {"script": script, "sample_count": cnt}
        # Remaining columns are (p50, p99) pairs in _MULTIPLE_SAT_EVIDENCE_KEYS order.
        for i, (feature, _key) in enumerate(_MULTIPLE_SAT_EVIDENCE_KEYS):
            p50 = float(row[2 + i * 2])
            p99 = float(row[2 + i * 2 + 1])
            rec[feature] = (p50, p99)
        results.append(rec)
    return results


# The nine attack-class score columns on tx_class_scores, in canonical order.
# Shared by the score-query builders below (filter validation, score_keys, and
# the per-class stats aggregation) so the list stays defined in one place.
_CLASS_COLS = (
    "token_dust", "large_value", "large_datum", "multiple_sat",
    "front_running", "sandwich", "circular", "fake_token", "phishing",
)


def _score_filter_conditions(
    network: str,
    risk_band: Optional[List[str]],
    attack_class: Optional[str],
    min_score: float,
    analyzed_from: Optional[Any],
    analyzed_to: Optional[Any],
    include_archived: bool,
    min_corroboration: int = 0,
) -> Tuple[List[str], Dict[str, Any]]:
    """Build the shared WHERE conditions + params for the class-scores list and
    count queries.

    Both ``get_class_scores_list`` and ``count_class_scores`` must apply the
    exact same filter, or pagination totals drift from the rows actually shown.
    Keeping the clause in one place guarantees they stay in sync. ``attack_class``
    is validated against ``_CLASS_COLS`` here (ValueError on an unknown value),
    so callers cannot inject an unvalidated class. Returns ``(conditions,
    params)``; the caller joins with " AND " and adds any query-specific params
    (e.g. limit/offset).
    """
    if attack_class and attack_class not in _CLASS_COLS:
        raise ValueError(f"Invalid attack_class '{attack_class}'")
    conditions = ["network = %(network)s"]
    params: Dict[str, Any] = {"network": network}
    if risk_band:
        # One named placeholder per value so the query is fully parameterized
        # (no string interpolation of user input); clickhouse-driver does not
        # expand a Python list into a SQL list automatically.
        placeholders = [f"%(risk_band_{i})s" for i in range(len(risk_band))]
        conditions.append(f"lower(risk_band) IN ({', '.join(placeholders)})")
        for i, rb in enumerate(risk_band):
            params[f"risk_band_{i}"] = rb.lower()
    if attack_class:
        # Filter by the DOMINANT class (max_class), not "this class has a
        # non-zero sub-score", so the list view's one-row-per-tx labelling stays
        # honest (a Phishing tx with a small circular score must not appear under
        # the Circular filter labelled Phishing).
        conditions.append("max_class = %(attack_class)s")
        params["attack_class"] = attack_class
    if min_score > 0:
        conditions.append("max_score >= %(min_score)s")
        params["min_score"] = min_score
    if min_corroboration > 0:
        # Multi-signal filter: only transactions where at least this many
        # distinct classes independently corroborated. Flag-only; orthogonal
        # to risk_band / max_score.
        conditions.append("corroboration_count >= %(min_corroboration)s")
        params["min_corroboration"] = min_corroboration
    if analyzed_from is not None:
        conditions.append("analyzed_at >= %(analyzed_from)s")
        params["analyzed_from"] = analyzed_from
    if analyzed_to is not None:
        conditions.append("analyzed_at < %(analyzed_to)s")
        params["analyzed_to"] = analyzed_to
    if not include_archived:
        # Anti-join via scalar subquery against currently-archived
        # (network, tx_hash) pairs. ClickHouse 26+ disallows FINAL on a table
        # inside a JOIN, so a subquery is used instead of a join.
        conditions.append(
            "(network, tx_hash) NOT IN ("
            "SELECT network, tx_hash FROM archived_alerts FINAL"
            ")"
        )
    return conditions, params


def get_class_scores_list(
    network: str,
    risk_band: Optional[List[str]] = None,
    attack_class: Optional[str] = None,
    min_score: float = 0.0,
    sort: str = "score",
    analyzed_from: Optional[Any] = None,
    analyzed_to: Optional[Any] = None,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
    min_corroboration: int = 0,
) -> List[Dict[str, Any]]:
    """Return multi-class score rows with optional filters.

    sort: "score" (default) or "date" (most recent first).
    include_archived: when False (default), rows whose (network, tx_hash) is
        present in ``archived_alerts`` are excluded so admin-curated false
        positives stop showing up in "currently dangerous" lists.
    risk_band: list of risk band values (case-insensitive). When non-empty,
        results are restricted via an ``IN`` clause; ``None`` or empty list
        means no filter.
    analyzed_from / analyzed_to: inclusive lower / exclusive upper bound on
    ``analyzed_at`` (datetime).
    """
    _ALLOWED_SORTS = {
        "score": "max_score DESC, analyzed_at DESC",
        "date": "analyzed_at DESC, max_score DESC",
    }
    order_clause = _ALLOWED_SORTS.get(sort, _ALLOWED_SORTS["score"])

    conditions, params = _score_filter_conditions(
        network, risk_band, attack_class, min_score,
        analyzed_from, analyzed_to, include_archived, min_corroboration,
    )
    params["limit"] = limit
    params["offset"] = offset

    where = " AND ".join(conditions)
    # Query scores first, then batch-fetch tx details separately.
    # ClickHouse 26+ does not allow FINAL on tables inside JOINs.
    rows = _get_client().execute(
        f"""
        SELECT tx_hash, network,
               token_dust, large_value, large_datum, multiple_sat,
               front_running, sandwich, circular, fake_token, phishing,
               max_score, max_class, risk_band, sub_scores, evidence,
               corroboration_count, corroborating_classes,
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
        "max_score", "max_class", "risk_band", "sub_scores", "evidence",
        "corroboration_count", "corroborating_classes",
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
    results = []
    for row in rows:
        d = dict(zip(score_keys, row))
        detail = tx_details.get(d["tx_hash"], {})
        d["fee"] = detail.get("fee")
        d["output_count"] = detail.get("output_count")
        for json_key in ("sub_scores", "evidence"):
            if isinstance(d.get(json_key), str):
                try:
                    d[json_key] = json.loads(d[json_key])
                except (json.JSONDecodeError, TypeError):
                    d[json_key] = {}
        results.append(d)
    return results


async def get_class_scores_list_async(
    network: str,
    risk_band: Optional[List[str]],
    attack_class: Optional[str],
    min_score: float, sort: str = "score", limit: int = 100, offset: int = 0,
    include_archived: bool = False,
    analyzed_from: Optional[Any] = None, analyzed_to: Optional[Any] = None,
    min_corroboration: int = 0,
) -> List[Dict[str, Any]]:
    # Bind by keyword so a future reorder of the sync signature can't silently
    # shuffle limit/offset into analyzed_from/analyzed_to (or vice versa).
    return await _in_executor(partial(
        get_class_scores_list,
        network=network,
        risk_band=risk_band,
        attack_class=attack_class,
        min_score=min_score,
        sort=sort,
        analyzed_from=analyzed_from,
        analyzed_to=analyzed_to,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
        min_corroboration=min_corroboration,
    ))


def count_class_scores(
    network: str,
    risk_band: Optional[List[str]] = None,
    attack_class: Optional[str] = None,
    min_score: float = 0.0,
    analyzed_from: Optional[Any] = None,
    analyzed_to: Optional[Any] = None,
    include_archived: bool = False,
    min_corroboration: int = 0,
) -> int:
    """Total number of class-score rows matching the given filters.

    Mirrors the WHERE clause of ``get_class_scores_list`` so the count is
    consistent with what would be returned (ignoring LIMIT/OFFSET).
    risk_band: list of bands; ``None`` or empty list means no filter. See
        ``get_class_scores_list`` for the same semantics.
    include_archived: when False (default), exclude rows whose
        ``(network, tx_hash)`` is present in ``archived_alerts`` — keeps the
        count aligned with the rows actually surfaced by the list query.
    """
    conditions, params = _score_filter_conditions(
        network, risk_band, attack_class, min_score,
        analyzed_from, analyzed_to, include_archived, min_corroboration,
    )

    where = " AND ".join(conditions)
    rows = _get_client().execute(
        f"SELECT count() FROM tx_class_scores FINAL WHERE {where}",
        params,
    )
    return int(rows[0][0]) if rows else 0


async def count_class_scores_async(
    network: str,
    risk_band: Optional[List[str]],
    attack_class: Optional[str],
    min_score: float,
    analyzed_from: Optional[Any] = None, analyzed_to: Optional[Any] = None,
    include_archived: bool = False,
    min_corroboration: int = 0,
) -> int:
    return await _in_executor(partial(
        count_class_scores,
        network=network,
        risk_band=risk_band,
        attack_class=attack_class,
        min_score=min_score,
        analyzed_from=analyzed_from,
        analyzed_to=analyzed_to,
        include_archived=include_archived,
        min_corroboration=min_corroboration,
    ))


async def get_class_scores_async(tx_hash: str) -> Optional[Dict[str, Any]]:
    return await _in_executor(get_class_scores, tx_hash)


def get_class_scores_stats(network: str, include_archived: bool = False) -> Dict[str, Any]:
    """Per-class distribution stats for a network.

    include_archived: when False (default), exclude rows whose (network, tx_hash)
        has been admin-archived so band counts reflect only currently-flagged txs.
    """
    # Build per-class aggregation: count of scored (>= 0), avg, max
    agg_parts = []
    for col in _CLASS_COLS:
        agg_parts.append(
            f"countIf({col} >= 0) AS {col}_count, "
            f"avgIf({col}, {col} >= 0) AS {col}_avg, "
            f"maxIf({col}, {col} >= 0) AS {col}_max"
        )
    agg_sql = ", ".join(agg_parts)
    archive_clause = (
        " AND (network, tx_hash) NOT IN ("
        "SELECT network, tx_hash FROM archived_alerts FINAL)"
        if not include_archived else ""
    )
    rows = _get_client().execute(
        f"""
        SELECT count() AS total,
               countIf(lower(risk_band) = 'critical') AS critical_count,
               countIf(lower(risk_band) = 'high') AS high_count,
               countIf(lower(risk_band) = 'moderate') AS moderate_count,
               -- 'low' is the pre-2026-06 label for the Informational band;
               -- counted here too so the stat stays correct mid-migration.
               countIf(lower(risk_band) IN ('informational', 'low')) AS informational_count,
               avg(max_score) AS avg_max_score,
               max(analyzed_at) AS last_analyzed_at,
               {agg_sql}
        FROM tx_class_scores FINAL
        WHERE network = %(network)s{archive_clause}
        """,
        {"network": network},
    )
    if not rows:
        return {}

    import math

    def _safe(v):
        """Convert NaN/inf floats (ClickHouse empty-agg artefacts) to None."""
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    # Read the single result row by column name rather than positional offsets.
    # The name list mirrors the SELECT order above: the fixed head columns, then
    # three aggregate columns (count, avg, max) per class. Zipping into a dict
    # removes the fragile row[idx+N] / idx+=3 arithmetic that silently breaks if
    # a SELECT column is added or reordered.
    _HEAD_COLS = (
        "total", "critical_count", "high_count", "moderate_count",
        "informational_count", "avg_max_score", "last_analyzed_at",
    )
    agg_cols = [f"{col}_{stat}" for col in _CLASS_COLS for stat in ("count", "avg", "max")]
    d = dict(zip([*_HEAD_COLS, *agg_cols], rows[0]))

    result: Dict[str, Any] = {
        "total": d["total"],
        "critical_count": d["critical_count"],
        "high_count": d["high_count"],
        "moderate_count": d["moderate_count"],
        "informational_count": d["informational_count"],
        "avg_max_score": _safe(d["avg_max_score"]),
        "last_analyzed_at": d["last_analyzed_at"],
    }
    result["per_class"] = {
        col: {
            "scored_count": d[f"{col}_count"],
            "avg_score": _safe(d[f"{col}_avg"]),
            "max_score": _safe(d[f"{col}_max"]),
        }
        for col in _CLASS_COLS
    }
    result["pending_count"] = get_pending_count(network)
    return result


def get_pending_count(network: str) -> int:
    """Count transactions ingested but not yet scored, on a like-for-like
    basis.

    The dashboard previously derived "pending" as
    ``count(transactions) - count(tx_class_scores)``, but those two counts
    aren't comparable: ``transactions`` is a plain MergeTree counted without
    FINAL (so re-ingested/reorg duplicates inflate it) while the scores count
    is FINAL-deduped AND archive-filtered (so every archived alert showed as
    permanently "pending").

    This computes the real backlog as the difference of two deduped counts:
    distinct ingested tx_hashes minus distinct scored tx_hashes. Every scored
    tx_hash is necessarily one we ingested (``scored ⊆ ingested``), so the
    difference is exactly the unscored set — without the cost of a per-row
    ``NOT IN`` against the full scored-hash set on every 15s poll.

    Notes:
      - No archive filter on the scores count: archived txs *were* scored, so
        they must not count as pending. (Distinct from the band-count stats,
        which exclude archived.)
      - ``greatest(0, ...)`` guards the rare case of a score row without a
        matching transactions row (e.g. cross-instance score import), which
        would otherwise drive the figure negative.
      - Input-deferred txs (awaiting transaction_inputs) have no score row yet
        and are correctly counted as pending.
    """
    rows = _get_client().execute(
        """
        SELECT greatest(0,
            (SELECT countDistinct(tx_hash) FROM transactions
             WHERE network = %(network)s)
            - (SELECT count() FROM tx_class_scores FINAL
               WHERE network = %(network)s)
        )
        """,
        {"network": network},
    )
    return int(rows[0][0]) if rows else 0


async def get_class_scores_stats_async(
    network: str, include_archived: bool = False,
) -> Dict[str, Any]:
    return await _in_executor(get_class_scores_stats, network, include_archived)


def get_alert_timeseries(
    network: str, days: int = 14, include_archived: bool = False,
) -> List[Dict[str, Any]]:
    """Daily count of High+Critical alerts over the last ``days`` days.

    Bucketed on the transaction's on-chain block ``timestamp`` (not
    ``analyzed_at``) so the trend reflects when attacks actually occurred,
    not our scoring/backfill cadence. Powers the dashboard sparkline.

    Excludes admin-archived rows by default so the trend matches the
    Critical KPI card (which also excludes them).

    FINAL is applied inside subqueries rather than on the joined tables
    directly: ClickHouse 26+ rejects FINAL on a table inside a JOIN.
    Gaps (days with zero alerts) are filled with 0 via ``WITH FILL`` so
    the sparkline renders a continuous line instead of collapsing missing
    days.

    Counts ``DISTINCT s.tx_hash`` rather than join-rows: the ``transactions``
    table is a plain MergeTree (no dedup), so a tx ingested more than once
    (chain reorg / re-sync) has duplicate rows that would otherwise fan out
    the JOIN and inflate the daily count. A tx_hash maps to exactly one
    block, so distinct-by-hash is the correct unit.
    """
    archive_clause = (
        " AND (network, tx_hash) NOT IN ("
        "SELECT network, tx_hash FROM archived_alerts FINAL)"
        if not include_archived else ""
    )
    rows = _get_client().execute(
        f"""
        SELECT toDate(t.timestamp) AS day, count(DISTINCT s.tx_hash) AS cnt
        FROM (
            SELECT tx_hash, network
            FROM tx_class_scores FINAL
            WHERE network = %(network)s
              AND lower(risk_band) IN ('high', 'critical')
              {archive_clause}
        ) AS s
        INNER JOIN (
            SELECT tx_hash, network, timestamp
            FROM transactions
            WHERE network = %(network)s
              AND timestamp >= toStartOfDay(now() - INTERVAL %(days)s DAY)
        ) AS t
          ON s.tx_hash = t.tx_hash AND s.network = t.network
        GROUP BY day
        ORDER BY day WITH FILL
            FROM toDate(now() - INTERVAL %(days)s DAY)
            TO toDate(now()) + 1
            STEP 1
        """,
        {"network": network, "days": days},
    )
    return [{"date": r[0].isoformat(), "count": int(r[1])} for r in rows]


async def get_alert_timeseries_async(
    network: str, days: int = 14, include_archived: bool = False,
) -> List[Dict[str, Any]]:
    return await _in_executor(get_alert_timeseries, network, days, include_archived)


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


def get_unanalyzed_transactions(
    network: str,
    batch_size: int,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return transactions that have no multi-class score yet.

    Fetches raw_data alongside the standard fields so that the feature
    extraction pipeline can derive UTxO-level and script-level features
    without a second round-trip.

    Defers a tx until ``transaction_inputs`` rows for it are visible. The
    ingester writes ``transactions`` and ``transaction_inputs`` as separate
    ``INSERT`` statements (ClickHouse has no multi-statement transactions;
    see :func:`insert_transactions_batch` for the writer side), so a poll
    that lands between the two writes would see the tx with no resolved
    input addresses, the scorer enrichment would no-op, and gate conditions
    like ``≥2 inputs from same script`` would silently fail. Per-statement
    atomicity guarantees that if any ``transaction_inputs`` row exists for
    the tx, all of them do; "any row exists" is therefore a sufficient
    witness that the inputs side is ready. Txs with ``input_count = 0``
    (treasury / collateral-only edge cases) are admitted directly since
    they need no input enrichment.

    ``since`` is the engine's watermark cursor (see engine._poll_since).
    Without it, every poll anti-joins the ENTIRE transactions table against
    the ENTIRE tx_class_scores table plus an unbounded transaction_inputs
    subquery: cost grows with total table size, not backlog size. With it,
    all three sides are bounded by ingestion/analysis time. Soundness:
    child transaction_inputs rows carry the parent tx's ingestion_timestamp
    (passed explicitly by the writer), and a tx ingested at >= since can
    only have been scored at analyzed_at >= since (scoring follows
    ingestion on the same host clock). The engine's periodic since=None
    full rescan is the never-skip safety net.
    """
    since_bound = "AND t.ingestion_timestamp >= %(since)s" if since else ""
    scores_bound = "AND analyzed_at >= %(since)s" if since else ""
    inputs_bound = "AND ingestion_timestamp >= %(since)s" if since else ""
    params: Dict[str, Any] = {"network": network, "batch_size": batch_size}
    if since:
        params["since"] = since
    rows = _get_client().execute(
        f"""
        SELECT t.tx_hash, t.network, t.fee, t.input_count, t.output_count,
               t.total_output_value, t.metadata, t.addresses, t.raw_data,
               t.raw_data_truncated, t.slot, t.block_height, t.timestamp,
               t.ingestion_timestamp
        FROM transactions t
        LEFT ANTI JOIN (
            SELECT tx_hash, network FROM tx_class_scores
            WHERE network = %(network)s {scores_bound}
        ) s
          ON t.tx_hash = s.tx_hash AND t.network = s.network
        WHERE t.network = %(network)s
          {since_bound}
          AND (t.input_count = 0
               OR t.tx_hash IN (
                   SELECT tx_hash FROM transaction_inputs
                   WHERE network = %(network)s {inputs_bound}
               ))
        ORDER BY t.ingestion_timestamp ASC
        LIMIT %(batch_size)s
        """,
        params,
    )
    keys = ("tx_hash", "network", "fee", "input_count", "output_count",
            "total_output_value", "metadata", "addresses", "raw_data",
            "raw_data_truncated", "slot", "block_height", "timestamp",
            "ingestion_timestamp")
    return [dict(zip(keys, row)) for row in rows]
