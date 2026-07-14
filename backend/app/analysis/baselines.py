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

# Drift guard (baselines.drift in config/detection.yaml): a recompute that
# drifts beyond the threshold in a RECALL-HARMFUL direction (p99 widening,
# p50 rising; BOTH directions for INVERTED_CONSUMER_FEATURES) is HELD
# (prior row stays active); recall-safe drifts apply.
# All drift events are logged to baseline_drift_events. This is the
# anti-poisoning control for per-script baselines; see the config comment.
_DRIFT_CFG = baselines_config()["drift"]
_DRIFT_ENABLED: bool = bool(_DRIFT_CFG["enabled"])
_DRIFT_P99_THRESHOLD: float = float(_DRIFT_CFG["p99_threshold"])
_DRIFT_P50_THRESHOLD: float = float(_DRIFT_CFG["p50_threshold"])

# Computation windows (baselines.windows in config/detection.yaml): the one
# source of truth shared with the retention warnings in clickhouse_schema.
_WINDOWS_CFG = baselines_config()["windows"]
_GLOBAL_WINDOW_DAYS: int = int(_WINDOWS_CFG["global_days"])
_PER_SCRIPT_WINDOW_DAYS: int = int(_WINDOWS_CFG["per_script_days"])

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

# Features consumed through normalise_inverted() by at least one scorer
# ("low value is suspicious" axes): ada_amount feeds token_dust's s_ada and
# large_value's s_ada; value_cbor_bytes feeds large_datum's s_value_inv
# (value_cbor_bytes also has plain-normalise consumers in token_dust and
# large_value, which is why membership means ANY inverted consumer: the
# drift hold must protect the weakest axis). For these features a FALLING
# p50/p99 is recall-harmful too: an attacker dumping low-ADA / small-CBOR
# outputs at a victim script drags the learned percentiles DOWN, and a
# lower (p50, p99) window de-sensitises the inverted axes (a dust output no
# longer sits "below normal", so s_ada / s_value_inv collapse to 0). The
# drift guard therefore holds BOTH directions for these features, while
# pure-normalise features keep the direction-aware hold (falling values
# there are strictly more sensitive). Trade-off accepted recall-first: a
# poisoned FIRST baseline on these features now needs analyst recovery
# (every drift event is logged to baseline_drift_events), because an honest
# shrinking recompute is held too. Other inverted consumers
# (multiple_sat's exunits_per_script_input, fake_token's
# mint_to_recipient_ratio) never receive learned baselines from this module
# (they resolve to bootstrap anchors), so they are not listed; add them
# here if baseline computation for them is ever introduced.
INVERTED_CONSUMER_FEATURES = frozenset({"ada_amount", "value_cbor_bytes"})

# Denominator guard for the relative drift ratio. The old == 0 case is
# short-circuited in check_drift, but _drift_ratio is also called when
# logging drift events where old can be 0; the epsilon avoids a
# ZeroDivisionError there. It sits many orders of magnitude below every
# baselined feature's scale (bytes, counts, lovelace), so it cannot perturb
# a comparison against the ~0.50 drift thresholds.
_DRIFT_RATIO_EPSILON = 1e-9


def compute_global_baselines(network: str) -> List[tuple]:
    """Compute global baselines from the utxo_features table (180-day window).

    Returns rows ready for insert_baselines().
    """
    now = datetime.now(timezone.utc)
    rows = []

    for feature in _UTXO_FEATURES:
        result = _query_percentiles(
            "utxo_features",
            feature,
            network,
            _GLOBAL_WINDOW_DAYS,
        )
        if result is None:
            continue
        p50, p99, count = result
        rows.append(
            (
                network,
                "global",
                "__global__",
                feature,
                p50,
                p99,
                count,
                now,
                _GLOBAL_WINDOW_DAYS,
            )
        )

    for feature in _TX_FEATURES:
        result = _query_percentiles(
            "tx_script_features",
            feature,
            network,
            _GLOBAL_WINDOW_DAYS,
        )
        if result is None:
            continue
        p50, p99, count = result
        rows.append(
            (
                network,
                "global",
                "__global__",
                feature,
                p50,
                p99,
                count,
                now,
                _GLOBAL_WINDOW_DAYS,
            )
        )

    rows = _filter_drifted(rows)
    if rows:
        clickhouse.insert_baselines(rows)
        logger.info(f"Baselines [global/{network}]: computed {len(rows)} features")
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
            "utxo_features",
            feature,
            network,
            "address",
            script_hash,
            _PER_SCRIPT_WINDOW_DAYS,
        )
        if result is None or result[2] < settings.BASELINE_MIN_SAMPLES:
            continue
        p50, p99, count = result
        rows.append(
            (
                network,
                "per_script",
                script_hash,
                feature,
                p50,
                p99,
                count,
                now,
                _PER_SCRIPT_WINDOW_DAYS,
            )
        )

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
        network,
        _PER_SCRIPT_WINDOW_DAYS,
        settings.BASELINE_MIN_SAMPLES,
    )
    rows = []
    for rec in per_script:
        script = rec["script"]
        count = rec["sample_count"]
        for feature in _MULTIPLE_SAT_PER_SCRIPT_FEATURES:
            p50, p99 = rec[feature]
            rows.append(
                (
                    network,
                    "per_script",
                    script,
                    feature,
                    p50,
                    p99,
                    count,
                    now,
                    _PER_SCRIPT_WINDOW_DAYS,
                )
            )

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
        network,
        "global",
        "__global__",
        "value_cbor_bytes",
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
                  AND timestamp >= now() - INTERVAL %(days)s DAY
            ) t ON f.tx_hash = t.tx_hash AND f.network = t.network
            GROUP BY f.address
            HAVING cnt >= %(min_samples)s
            ORDER BY cnt DESC
            LIMIT %(limit)s
            """,
            {
                "network": network,
                "days": _PER_SCRIPT_WINDOW_DAYS,
                "min_samples": settings.BASELINE_MIN_SAMPLES,
                "limit": limit,
            },
        )
        return [r[0] for r in rows]
    except Exception:
        logger.exception("Failed to get active script addresses")
        return []


def recompute_all_baselines(network: str, max_scripts: int = 500) -> int:
    """Recompute global + per-script baselines. Returns total rows written.

    Note: the ``per_policy`` baseline tier (per minting-policy percentiles for
    large_value.quantity_digits, fake_token.recipient_count /
    mint_to_recipient_ratio, sandwich.price_impact / swap_profit) is NOT
    computed here. Those features ``_resolve('per_policy', ...)`` and therefore
    fall through to the conservative bootstrap anchors in detection.yaml. This
    is deliberate and recall-safe (bootstrap anchors never de-sensitise); the
    per-policy adaptation is deferred until there is enough per-policy volume to
    fit stable percentiles. See docs/TMS_DETECTION_SPEC.md (baselines)."""
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
    threshold: float,
) -> bool:
    """Return True if the new value drifted beyond threshold from the old.

    Symmetric magnitude check (works for any percentile); the HOLD decision
    is direction-aware and lives in ``_filter_drifted``. ``threshold`` is
    deliberately required (no default): the tunable lives in
    ``baselines.drift`` in config/detection.yaml and must flow through the
    validated loader, not a hardcoded fallback.
    """
    if old_p99 == 0:
        return new_p99 > 0
    return abs(new_p99 - old_p99) / (abs(old_p99) + _DRIFT_RATIO_EPSILON) > threshold


def _drift_ratio(old: float, new: float) -> float:
    return abs(new - old) / (abs(old) + _DRIFT_RATIO_EPSILON)


def _filter_drifted(rows: List[tuple]) -> List[tuple]:
    """Hold baseline recomputes that drift in a RECALL-HARMFUL direction.

    Direction-aware for pure-normalise features: only de-sensitising moves
    are held, i.e. a WIDENING p99 (new > old) or a RISING p50 (the
    median-poisoning vector: normalise() subtracts p50 first). A narrowing
    p99 or falling p50 makes plain-normalise detection strictly more
    sensitive and applies; holding those made a poisoned first baseline
    self-protecting, because every honest recompute that shrank it back was
    itself a >threshold change. A prior with p99 == 0 never holds:
    _baseline_is_usable already rejects it at resolution time, so it
    protects nothing.

    Features in ``INVERTED_CONSUMER_FEATURES`` hold BOTH directions:
    "falling is recall-safe" is false for normalise_inverted() consumers,
    where a downward-poisoned window zeroes the inverted axis (see the
    constant's comment for the threat model).

    Held rows are NOT inserted (the ReplacingMergeTree keeps the prior row
    as the active baseline). Every drift event, held or applied, is
    recorded in ``baseline_drift_events`` (axis + applied flag) so an
    analyst can review; held ones also log a warning naming the axis or
    axes that caused the hold.  First-ever baselines (no prior row) always
    pass: drift is only definable against history.

    Row tuple shape: ``(network, scope_type, scope_id, feature, p50, p99,
    sample_count, computed_at, window_days)``.
    """
    if not _DRIFT_ENABLED:
        return rows
    kept: List[tuple] = []
    for row in rows:
        network, scope_type, scope_id, feature, new_p50, new_p99 = row[:6]
        new_p50, new_p99 = float(new_p50), float(new_p99)
        computed_at = row[7]
        prior = clickhouse.get_baseline(network, scope_type, scope_id, feature)
        if prior is None:
            kept.append(row)
            continue
        old_p50, old_p99 = float(prior["p50"]), float(prior["p99"])

        # Symmetric hold for features with an inverted consumer: BOTH
        # directions of drift are recall-harmful there.
        symmetric_hold = feature in INVERTED_CONSUMER_FEATURES
        p99_drifted = check_drift(old_p99, new_p99, _DRIFT_P99_THRESHOLD)
        p50_drifted = check_drift(old_p50, new_p50, _DRIFT_P50_THRESHOLD)
        p99_hold = p99_drifted and old_p99 > 0 and (new_p99 > old_p99 or symmetric_hold)
        p50_hold = p50_drifted and (new_p50 > old_p50 or symmetric_hold)
        held = p99_hold or p50_hold

        drifted_axes = []
        held_axes = []
        if p99_drifted:
            drifted_axes.append(("p99", old_p99, new_p99))
            if p99_hold:
                held_axes.append(("p99", old_p99, new_p99))
        if p50_drifted:
            drifted_axes.append(("p50", old_p50, new_p50))
            if p50_hold:
                held_axes.append(("p50", old_p50, new_p50))
        for axis, old_v, new_v in drifted_axes:
            try:
                clickhouse.insert_baseline_drift_event(
                    network,
                    scope_type,
                    scope_id,
                    feature,
                    old_v,
                    new_v,
                    _drift_ratio(old_v, new_v),
                    computed_at,
                    axis=axis,
                    applied=not held,
                )
            except Exception:
                logger.exception("Failed to record baseline drift event")

        if held:
            # Name the axis/values that actually CAUSED the hold (there can
            # be two); an axis that merely drifted in an applied-safe
            # direction must not be reported as the cause.
            detail = "; ".join(
                f"{axis} {old_v:.4g} -> {new_v:.4g} (ratio {_drift_ratio(old_v, new_v):.2f})"
                for axis, old_v, new_v in held_axes
            )
            logger.warning(
                "Baseline drift HELD on %s: %s/%s %s/%s; prior baseline stays active",
                detail,
                scope_type,
                scope_id[:16],
                network,
                feature,
            )
            continue
        if drifted_axes:
            logger.info(
                "Baseline drift applied (recall-safe direction): %s/%s %s/%s",
                scope_type,
                scope_id[:16],
                network,
                feature,
            )
        kept.append(row)
    return kept


def _query_percentiles(
    table: str,
    feature: str,
    network: str,
    window_days: int,
    *,
    scope_column: Optional[str] = None,
    scope_value: Optional[str] = None,
) -> Optional[Tuple[float, float, int]]:
    """Query p50 and p99 for a feature over the chain-time window.

    With ``scope_column``/``scope_value`` set, the percentiles are restricted to a
    single address or policy; the scope predicate is applied INSIDE the feature
    subquery (before the JOIN) so it filters which feature rows feed the quantiles
    and never touches the chain-time window side. Returns (p50, p99, sample_count)
    or None if no data.
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Disallowed table: {table}")
    if feature not in _ALLOWED_FEATURES:
        raise ValueError(f"Disallowed feature: {feature}")
    scope_sql = ""
    params = {"network": network, "days": window_days}
    if scope_column is not None:
        if scope_column not in _ALLOWED_SCOPE_COLUMNS:
            raise ValueError(f"Disallowed scope_column: {scope_column}")
        scope_sql = f"\n                  AND {scope_column} = %(scope_value)s"
        params["scope_value"] = scope_value
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
        # INNER JOIN deliberately: a feature row without a transactions row
        # has no chain timestamp, and a LEFT JOIN with an ingestion-time
        # fallback would reintroduce exactly the backfill distortion this
        # JOIN exists to prevent. The retention coupling (transactions
        # retention shorter than the window silently shrinks the sample)
        # is surfaced by the warning in clickhouse_schema.apply_retention_ttls.
        # The optional scope predicate lives in the feature subquery so it never
        # filters the chain-time window side.
        rows = client.execute(
            f"""
            SELECT
                quantileExact(0.50)(toFloat64(f.{feature})) AS p50,
                quantileExact(0.99)(toFloat64(f.{feature})) AS p99,
                count() AS cnt
            FROM (
                SELECT * FROM {table} FINAL
                WHERE network = %(network)s{scope_sql}
            ) f
            JOIN (
                SELECT tx_hash, network, timestamp FROM transactions FINAL
                WHERE network = %(network)s
                  AND timestamp >= now() - INTERVAL %(days)s DAY
            ) t ON f.tx_hash = t.tx_hash AND f.network = t.network
            WHERE t.timestamp >= now() - INTERVAL %(days)s DAY
            """,
            params,
        )
        if rows and rows[0][2] > 0:
            return float(rows[0][0]), float(rows[0][1]), int(rows[0][2])
    except Exception:
        scope = "" if scope_column is None else f" @ {scope_column}={str(scope_value)[:16]}"
        logger.exception(f"Failed to query percentiles for {table}.{feature}{scope}")
    return None


def _query_percentiles_scoped(
    table: str,
    feature: str,
    network: str,
    scope_column: str,
    scope_value: str,
    window_days: int,
) -> Optional[Tuple[float, float, int]]:
    """Per-address/policy p50/p99 (thin wrapper over _query_percentiles)."""
    return _query_percentiles(
        table,
        feature,
        network,
        window_days,
        scope_column=scope_column,
        scope_value=scope_value,
    )
