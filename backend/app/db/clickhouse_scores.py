"""tx_class_scores read/write layer.

Split out of ``app.db.clickhouse`` (which retains connection plumbing,
ingestion writers, and baseline I/O): everything here serves the scoring
pipeline and its API surface — the score-vector writer, the
archive-aware list/count/stats/timeseries readers, the multiple_sat
extraction percentiles, and the unanalyzed-transactions poll.

Client and executor access goes through the facade AT CALL TIME (the
function-level imports below): tests and tooling monkeypatch
``app.db.clickhouse._get_client`` / ``_ch_executor``, and a module-level
import here would freeze the unpatched references. The facade re-exports
every public name in this module, so callers keep using
``clickhouse.insert_class_scores`` etc. unchanged.
"""

import json
import logging
from datetime import datetime
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _client():
    """The facade's per-thread Client, resolved late (monkeypatch-safe)."""
    from app.db import clickhouse
    return clickhouse._get_client()


async def _run(fn, *args):
    """Run ``fn`` on the facade's ClickHouse executor, resolved late."""
    from app.db import clickhouse
    return await clickhouse._in_executor(fn, *args)


def insert_class_scores(results: List[Dict[str, Any]]):
    """Batch-insert multi-class scoring results into tx_class_scores."""
    if not results:
        return
    _client().execute(
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
    rows = _client().execute(
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

    rows = _client().execute(
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
    rows = _client().execute(
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
        detail_rows = _client().execute(
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
    return await _run(partial(
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
    rows = _client().execute(
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
    return await _run(partial(
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
    return await _run(get_class_scores, tx_hash)


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
    rows = _client().execute(
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
    rows = _client().execute(
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
    return await _run(get_class_scores_stats, network, include_archived)


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
    rows = _client().execute(
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
    return await _run(get_alert_timeseries, network, days, include_archived)



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
    rows = _client().execute(
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
