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
from typing import List, Optional, Tuple

from app.analysis.scorer_config import baselines_config
from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

# Drift guard (baselines.drift in config/detection.yaml): a recompute whose
# p99 jumps beyond the threshold relative to the stored baseline is HELD
# (prior row stays active) and logged to baseline_drift_events. This is the
# anti-poisoning control for per-script baselines; see the config comment.
_DRIFT_CFG = baselines_config()["drift"]
_DRIFT_ENABLED: bool = bool(_DRIFT_CFG["enabled"])
_DRIFT_P99_THRESHOLD: float = float(_DRIFT_CFG["p99_threshold"])

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

# multiple_sat VALUE-extraction features (lovelace / native assets leaving the
# script). Computed only at scoring time (they need resolved inputs), so they
# are not in any ingestion feature table; their per-script baselines are
# aggregated from the persisted tx_class_scores.evidence instead (see
# clickhouse.query_multiple_sat_extraction_percentiles). Emitted at per_script
# scope ONLY: the global distribution is dominated by legitimate high-volume
# asset-movers, so a global baseline would de-sensitise detection on rare/novel
# scripts. multiple_sat resolves these per_script -> bootstrap, never global.
#
# ONLY the value axis is per-script-calibrated. exunits_per_script_input feeds an
# INVERTED signal (the lazy-validator floor), which is an absolute concept
# ("near-zero CPU"); a per-script baseline would make a script that consistently
# does heavy work look "lazy" relative to its own median and spuriously floor it
# to High. n_inputs is left on the absolute bootstrap for the same reason. Both
# stay on the original per_script->global->bootstrap resolution.
#
# Derived from clickhouse._MULTIPLE_SAT_EVIDENCE_KEYS, the single source of truth
# for which value features are per-script-calibrated (and which evidence JSON key
# carries each). Deriving rather than re-listing means the percentile query and
# this emitter cannot drift out of sync.
_MULTIPLE_SAT_PER_SCRIPT_FEATURES = [
    feature for feature, _evidence_key in clickhouse._MULTIPLE_SAT_EVIDENCE_KEYS
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

    rows = _filter_drifted(rows)
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

    rows = _filter_drifted(rows)
    if rows:
        clickhouse.insert_baselines(rows)
        logger.info(
            f"Baselines [per_script/{script_hash[:16]}...]: "
            f"computed {len(rows)} features ({rows[0][6]} samples)"
        )
    return rows


def compute_multiple_sat_per_script_baselines(network: str) -> List[tuple]:
    """Compute per-script baselines for the multiple_sat extraction features.

    Sourced from tx_class_scores.evidence (90-day window) rather than an
    ingestion feature table, because the extraction values are only computed at
    scoring time. Emits per_script rows ONLY (no global): see
    ``_MULTIPLE_SAT_PER_SCRIPT_FEATURES`` for why. Scripts below
    BASELINE_MIN_SAMPLES are skipped by the query, so rare/novel scripts (where
    one-shot double-sat exploits live) keep falling back to the conservative
    bootstrap anchor.

    Cold-start note: the source (evidence) is produced BY scoring, which consumes
    these baselines, so on a fresh DB the correct order is reclassify (populate
    evidence on bootstrap) -> recompute baselines (this) -> reclassify (apply the
    per-script de-saturation). It is stable across cycles: an asset-extracting
    spend keeps scoring >= 0 and so keeps emitting evidence even after it
    de-saturates, so the per-script population does not collapse.
    """
    now = datetime.now(timezone.utc)
    per_script = clickhouse.query_multiple_sat_extraction_percentiles(
        network, 90, settings.BASELINE_MIN_SAMPLES,
    )
    rows = []
    for rec in per_script:
        script = rec["script"]
        count = rec["sample_count"]
        for feature in _MULTIPLE_SAT_PER_SCRIPT_FEATURES:
            p50, p99 = rec[feature]
            rows.append((
                network, "per_script", script, feature,
                p50, p99, count, now, 90,
            ))

    rows = _filter_drifted(rows)
    if rows:
        clickhouse.insert_baselines(rows)
        logger.info(
            f"Baselines [per_script/multiple_sat/{network}]: "
            f"{len(rows)} feature-rows across {len(per_script)} scripts"
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
        # Chain-time window + FINAL dedup: same rationale as
        # _query_percentiles; cnt gates BASELINE_MIN_SAMPLES, so duplicate
        # rows must not inflate it.
        rows = client.execute(
            """
            SELECT f.address AS address, count() AS cnt
            FROM (
                SELECT tx_hash, network, address FROM utxo_features FINAL
                WHERE network = %(network)s AND is_script_address = 1
            ) f
            JOIN (
                SELECT tx_hash, network, timestamp FROM transactions FINAL
                WHERE network = %(network)s
                  AND timestamp >= now() - INTERVAL 90 DAY
            ) t ON f.tx_hash = t.tx_hash AND f.network = t.network
            GROUP BY f.address
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

    # Per-script extraction baselines for multiple_sat (sourced from evidence,
    # per_script-only). One grouped query rather than per-script, so it is not
    # bounded by max_scripts.
    try:
        total += len(compute_multiple_sat_per_script_baselines(network))
    except Exception:
        logger.exception("Failed to recompute multiple_sat per-script baselines")

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


def _filter_drifted(rows: List[tuple]) -> List[tuple]:
    """Hold baseline updates whose p99 drifted beyond the configured
    threshold from the stored baseline.

    Held rows are NOT inserted (the ReplacingMergeTree keeps the prior row
    as the active baseline) and are recorded in ``baseline_drift_events``
    plus a warning log so an analyst can review and, if legitimate, apply
    them by re-running the recompute after raising the threshold or
    clearing the stored row. First-ever baselines (no prior row) always
    pass: drift is only definable against history.

    Row tuple shape: ``(network, scope_type, scope_id, feature, p50, p99,
    sample_count, computed_at, window_days)``.
    """
    if not _DRIFT_ENABLED:
        return rows
    kept: List[tuple] = []
    for row in rows:
        network, scope_type, scope_id, feature, _p50, new_p99 = row[:6]
        computed_at = row[7]
        prior = clickhouse.get_baseline(network, scope_type, scope_id, feature)
        if prior is None:
            kept.append(row)
            continue
        old_p99 = float(prior["p99"])
        if check_drift(old_p99, float(new_p99), _DRIFT_P99_THRESHOLD):
            drift_ratio = (
                abs(float(new_p99) - old_p99) / (abs(old_p99) + 1e-9)
            )
            try:
                clickhouse.insert_baseline_drift_event(
                    network, scope_type, scope_id, feature,
                    old_p99, float(new_p99), drift_ratio, computed_at,
                )
            except Exception:
                logger.exception("Failed to record baseline drift event")
            logger.warning(
                "Baseline drift HELD: %s/%s %s/%s p99 %.4g -> %.4g "
                "(ratio %.2f > %.2f); prior baseline stays active",
                scope_type, scope_id[:16], network, feature,
                old_p99, float(new_p99), drift_ratio, _DRIFT_P99_THRESHOLD,
            )
            continue
        kept.append(row)
    return kept


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
        # Window on CHAIN time (transactions.timestamp), not ingestion time:
        # during a backfill/replay every historical row carries a recent
        # ingestion_timestamp, which would collapse the 90/180-day window to
        # "everything ingested recently" and distort the percentiles.
        # quantileExact for determinism: the default quantile() is a sampled
        # estimator whose output jitters across recomputes, which would feed
        # spurious drift signals (matches the multiple_sat precedent).
        # FINAL on both sides: a not-yet-merged duplicate feature row (or a
        # duplicate transactions row multiplying the join) would weight the
        # quantiles. Daily-batch cadence absorbs the FINAL cost.
        rows = client.execute(
            f"""
            SELECT
                quantileExact(0.50)(toFloat64(f.{feature})) AS p50,
                quantileExact(0.99)(toFloat64(f.{feature})) AS p99,
                count() AS cnt
            FROM (SELECT * FROM {table} FINAL WHERE network = %(network)s) f
            JOIN (
                SELECT tx_hash, network, timestamp FROM transactions FINAL
                WHERE network = %(network)s
                  AND timestamp >= now() - INTERVAL %(days)s DAY
            ) t ON f.tx_hash = t.tx_hash AND f.network = t.network
            WHERE t.timestamp >= now() - INTERVAL %(days)s DAY
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
        # Chain-time window + exact quantiles + FINAL dedup: same rationale
        # as _query_percentiles above.
        rows = client.execute(
            f"""
            SELECT
                quantileExact(0.50)(toFloat64(f.{feature})) AS p50,
                quantileExact(0.99)(toFloat64(f.{feature})) AS p99,
                count() AS cnt
            FROM (
                SELECT * FROM {table} FINAL
                WHERE network = %(network)s
                  AND {scope_column} = %(scope_value)s
            ) f
            JOIN (
                SELECT tx_hash, network, timestamp FROM transactions FINAL
                WHERE network = %(network)s
                  AND timestamp >= now() - INTERVAL %(days)s DAY
            ) t ON f.tx_hash = t.tx_hash AND f.network = t.network
            WHERE t.timestamp >= now() - INTERVAL %(days)s DAY
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
