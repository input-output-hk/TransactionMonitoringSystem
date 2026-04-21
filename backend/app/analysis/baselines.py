"""Baseline computation and bootstrap for the detection system.

Computes percentile statistics (p50, p99) from historical UTxO and transaction
features stored in ClickHouse.  Baselines are used by scorers to normalise raw
feature values into the 0-1 range via the Polimi percentile framework.

Baseline tiers:
  - per_script: 90-day window, per script address hash
  - per_policy: 90-day window, per minting policy ID
  - global: 180-day window, across all transactions

When a scope has fewer than BASELINE_MIN_SAMPLES transactions, scorers fall
back to the next broader tier (per_script -> global -> missing).
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

# Features computed from utxo_features table
_UTXO_FEATURES = [
    "value_cbor_bytes",
    "unique_token_count",
    "ada_amount",
    "datum_bytes",
    "utxo_total_bytes",
]

# Features computed from tx_script_features table
_TX_FEATURES = [
    "redeemers_count",
    "exunits_mem_total",
    "exunits_cpu_total",
]

# Allowed table names and column names for SQL identifier interpolation
_ALLOWED_TABLES = {"utxo_features", "tx_script_features"}
_ALLOWED_FEATURES = set(_UTXO_FEATURES + _TX_FEATURES)
_ALLOWED_SCOPE_COLUMNS = {"address", "policy_id"}


def compute_global_baselines(network: str) -> List[tuple]:
    """Compute global baselines from the utxo_features table (180-day window).

    Returns rows ready for insert_baselines().
    """
    now = datetime.now(timezone.utc)
    rows = []

    for feature in _UTXO_FEATURES:
        result = _query_percentiles("utxo_features", feature, network, 180)
        if result is None:
            continue
        p50, p99, count = result
        rows.append((
            network, "global", "__global__", feature,
            p50, p99, count, now, 180,
        ))

    for feature in _TX_FEATURES:
        result = _query_percentiles("tx_script_features", feature, network, 180)
        if result is None:
            continue
        p50, p99, count = result
        rows.append((
            network, "global", "__global__", feature,
            p50, p99, count, now, 180,
        ))

    if rows:
        clickhouse.insert_baselines(rows)
        logger.info(
            f"Baselines [global/{network}]: computed {len(rows)} features"
        )
    return rows


def compute_script_baselines(
    network: str,
    script_hash: str,
) -> List[tuple]:
    """Compute per-script baselines from utxo_features (90-day window)."""
    now = datetime.now(timezone.utc)
    rows = []

    for feature in _UTXO_FEATURES:
        result = _query_percentiles_scoped(
            "utxo_features", feature, network,
            "address", script_hash, 90,
        )
        if result is None or result[2] < settings.BASELINE_MIN_SAMPLES:
            continue
        p50, p99, count = result
        rows.append((
            network, "per_script", script_hash, feature,
            p50, p99, count, now, 90,
        ))

    if rows:
        clickhouse.insert_baselines(rows)
        logger.info(
            f"Baselines [per_script/{script_hash[:16]}...]: "
            f"computed {len(rows)} features ({rows[0][6]} samples)"
        )
    return rows


def bootstrap_baselines(network: str) -> int:
    """Bootstrap baselines if the baselines table is empty.

    Called at startup when BASELINE_BOOTSTRAP_ON_STARTUP is True.
    Returns the number of baseline rows created.
    """
    existing = clickhouse.get_baseline(
        network, "global", "__global__", "value_cbor_bytes",
    )
    if existing and existing["sample_count"] >= settings.BASELINE_MIN_SAMPLES:
        logger.info("Baselines already bootstrapped, skipping")
        return 0

    logger.info(f"Bootstrapping global baselines for {network}...")
    rows = compute_global_baselines(network)
    return len(rows)


def get_active_script_addresses(network: str, limit: int = 500) -> List[str]:
    """Return the most active script addresses from utxo_features (90-day window).

    Ordered by transaction count descending, limited to top N.
    """
    try:
        client = clickhouse._get_client()
        rows = client.execute(
            """
            SELECT address, count() AS cnt
            FROM utxo_features
            WHERE network = %(network)s
              AND is_script_address = 1
              AND ingestion_timestamp >= now() - INTERVAL 90 DAY
            GROUP BY address
            HAVING cnt >= %(min_samples)s
            ORDER BY cnt DESC
            LIMIT %(limit)s
            """,
            {
                "network": network,
                "min_samples": settings.BASELINE_MIN_SAMPLES,
                "limit": limit,
            },
        )
        return [r[0] for r in rows]
    except Exception:
        logger.exception("Failed to get active script addresses")
        return []


def recompute_all_baselines(network: str, max_scripts: int = 500) -> int:
    """Recompute global + per-script baselines. Returns total rows written."""
    total = 0

    # Global baselines
    rows = compute_global_baselines(network)
    total += len(rows)

    # Per-script baselines for active scripts
    scripts = get_active_script_addresses(network, limit=max_scripts)
    for addr in scripts:
        try:
            rows = compute_script_baselines(network, addr)
            total += len(rows)
        except Exception:
            logger.exception(f"Failed to recompute baselines for {addr[:16]}...")

    logger.info(f"Baseline recomputation complete: {total} rows for {len(scripts)} scripts")
    return total


def check_drift(
    old_p99: float,
    new_p99: float,
    threshold: float = 0.50,
) -> bool:
    """Return True if the new p99 drifted beyond threshold from the old one."""
    if old_p99 == 0:
        return new_p99 > 0
    return abs(new_p99 - old_p99) / (abs(old_p99) + 1e-9) > threshold


def _query_percentiles(
    table: str,
    feature: str,
    network: str,
    window_days: int,
) -> Optional[Tuple[float, float, int]]:
    """Query p50 and p99 for a feature from a ClickHouse table.

    Returns (p50, p99, sample_count) or None if no data.
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Disallowed table: {table}")
    if feature not in _ALLOWED_FEATURES:
        raise ValueError(f"Disallowed feature: {feature}")
    try:
        client = clickhouse._get_client()
        rows = client.execute(
            f"""
            SELECT
                quantile(0.50)(toFloat64({feature})) AS p50,
                quantile(0.99)(toFloat64({feature})) AS p99,
                count() AS cnt
            FROM {table}
            WHERE network = %(network)s
              AND ingestion_timestamp >= now() - INTERVAL %(days)s DAY
            """,
            {"network": network, "days": window_days},
        )
        if rows and rows[0][2] > 0:
            return float(rows[0][0]), float(rows[0][1]), int(rows[0][2])
    except Exception:
        logger.exception(f"Failed to query percentiles for {table}.{feature}")
    return None


def _query_percentiles_scoped(
    table: str,
    feature: str,
    network: str,
    scope_column: str,
    scope_value: str,
    window_days: int,
) -> Optional[Tuple[float, float, int]]:
    """Query p50 and p99 scoped to a specific address or policy."""
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Disallowed table: {table}")
    if feature not in _ALLOWED_FEATURES:
        raise ValueError(f"Disallowed feature: {feature}")
    if scope_column not in _ALLOWED_SCOPE_COLUMNS:
        raise ValueError(f"Disallowed scope_column: {scope_column}")
    try:
        client = clickhouse._get_client()
        rows = client.execute(
            f"""
            SELECT
                quantile(0.50)(toFloat64({feature})) AS p50,
                quantile(0.99)(toFloat64({feature})) AS p99,
                count() AS cnt
            FROM {table}
            WHERE network = %(network)s
              AND {scope_column} = %(scope_value)s
              AND ingestion_timestamp >= now() - INTERVAL %(days)s DAY
            """,
            {"network": network, "scope_value": scope_value, "days": window_days},
        )
        if rows and rows[0][2] > 0:
            return float(rows[0][0]), float(rows[0][1]), int(rows[0][2])
    except Exception:
        logger.exception(
            f"Failed to query scoped percentiles for "
            f"{table}.{feature} @ {scope_column}={scope_value[:16]}"
        )
    return None
