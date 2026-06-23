"""API endpoints for the multi-class Analysis Engine"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Security

from app.analysis import contract_anomaly as ca_projection
from app.analysis.contract_anomaly import corroboration_threshold
from app.analysis.engine import _CLASS_NAMES
from app.analysis.normalise import score_to_band
from app.auth import verify_api_key
from app.config import settings
from app.db import archive_queries, clickhouse, clustering_queries
from app.models.transaction import ClassScoreResult, NetworkType, RiskBand
from app.utils.datetime_utils import format_iso_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

# _CLASS_NAMES is imported from app.analysis.engine (the canonical scorer-order
# source) so the API's class validation can't drift from the engine's order.

# The synthetic class merged in at read time from the clustering sidecar. It is
# NOT in _CLASS_NAMES (which mirrors the nine hardcoded tx_class_scores columns)
# so the per-tx write path stays untouched; it is injected after hydration.
_CONTRACT_ANOMALY = "contract_anomaly"


def _merge_contract_anomaly(
    result: ClassScoreResult, rows: List[Dict[str, Any]],
) -> None:
    """Fold the clustering sidecar's verdict(s) for a tx into a hydrated result.

    ``rows`` are the raw per-(watched-contract) verdict rows; this resolves them
    to the highest-severity one (host-scale score computed from the projection
    config) and merges it additively. Recall-first: it only ever RAISES
    max_score / risk_band via max(...); it never lowers an existing class score
    and never mutates the stored, server-filterable corroboration_count (the
    contract_anomaly corroboration signal rides on its own boolean field).
    Mutates ``result`` in place; a no-op when ``rows`` is empty.
    """
    resolved = ca_projection.resolve(rows)
    if resolved is None:
        return
    score = float(resolved["score"])
    result.scores[_CONTRACT_ANOMALY] = score
    result.sub_scores[_CONTRACT_ANOMALY] = {
        "consensus": float(resolved.get("consensus") or 0.0),
        "votes": int(resolved.get("votes", 0) or 0),
        "cluster_id": int(resolved.get("cluster_id", -1)),
        "verdict": resolved.get("verdict", ""),
    }
    evidence = resolved.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    result.evidence[_CONTRACT_ANOMALY] = {
        **evidence,
        "target": resolved.get("target", ""),
        "model_id": resolved.get("model_id", ""),
        "feature_set": resolved.get("feature_set", ""),
    }
    if score > result.max_score:
        result.max_score = score
        result.max_class = _CONTRACT_ANOMALY
        result.risk_band = RiskBand(score_to_band(score))
    result.contract_anomaly_corroborates = score >= corroboration_threshold()
    result.contract_anomaly_scored_at = resolved.get("scored_at")


def _passes_score_band(
    score: float, band: RiskBand, min_score: float, bands: Optional[List[str]],
) -> bool:
    """Whether a (score, band) pair satisfies the list view's score/band filter.

    Mirrors the DB-side predicate in ``_score_filter_conditions`` (max_score >=
    min_score AND lower(risk_band) IN bands) so the contract_anomaly rescue
    admits exactly the rows the DB filter would have, had it seen the merged
    score. Empty/None ``bands`` means no band restriction."""
    if min_score > 0 and score < min_score:
        return False
    if bands and band.value.lower() not in {b.lower() for b in bands}:
        return False
    return True


def _within_analyzed_window(
    analyzed_at: Any, analyzed_from: Optional[datetime], analyzed_to: Optional[datetime],
) -> bool:
    """Mirror the DB analyzed_at bounds (>= from, < to) for a rescued row."""
    if analyzed_at is None:
        return analyzed_from is None and analyzed_to is None
    if analyzed_from is not None and analyzed_at < analyzed_from:
        return False
    if analyzed_to is not None and analyzed_at >= analyzed_to:
        return False
    return True


# Band severity ordering for the effective (stored vs contract_anomaly) compare.
# Higher rank = more severe. 'low' is the pre-2026-06 alias for Informational.
_BAND_RANK = {"critical": 4, "high": 3, "moderate": 2, "informational": 1, "low": 1}
# The stats count each band contributes to (mirrors get_class_scores_stats keys).
_BAND_COUNT_KEY = {
    "critical": "critical_count", "high": "high_count",
    "moderate": "moderate_count", "informational": "informational_count",
    "low": "informational_count",
}
# Bands the timeseries (and the Critical+High KPI) count as an alert.
_ALERT_BANDS = frozenset({"high", "critical"})


async def _flagged_effective_bands(
    network: str,
) -> Dict[str, tuple[str, str]]:
    """For every contract_anomaly-flagged tx on a network, return
    ``{tx_hash: (stored_band, effective_ca_band)}`` (both lowercase).

    The host counts/orders on the STORED 9-class band, so a tx whose sidecar
    verdict outranks its stored band is undercounted. This resolves each flagged
    tx's contract_anomaly band (via the same projection the merge uses) alongside
    its stored band so the read endpoints can reconcile to the effective band.
    Archived / unscored txs are absent (``get_class_scores_by_hashes`` applies the
    same archive anti-join the stats/timeseries do, so they stay excluded)."""
    flagged = await clustering_queries.flagged_for_network_async(network)
    if not flagged:
        return {}
    stored_rows = await clickhouse.get_class_scores_by_hashes_async(
        network, list(flagged),
    )
    stored_band = {r["tx_hash"]: str(r["risk_band"]).lower() for r in stored_rows}
    out: Dict[str, tuple[str, str]] = {}
    for tx, rows in flagged.items():
        sb = stored_band.get(tx)
        if sb is None:  # archived / unscored: excluded from the host aggregates
            continue
        resolved = ca_projection.resolve(rows)
        if resolved is None:
            continue
        out[tx] = (sb, resolved["risk_band"].value.lower())
    return out


async def _augment_stats_with_contract_anomaly(
    network: str, stats: Dict[str, Any],
) -> None:
    """Reclassify each flagged tx into its EFFECTIVE band in the band counts, so
    the KPI cards don't undercount Critical/High for txs whose contract_anomaly
    verdict outranks their stored band. Mutates ``stats`` (a fresh per-call copy
    from the cached aggregate). A no-op for txs already at/above their CA band."""
    for sb, cb in (await _flagged_effective_bands(network)).values():
        if _BAND_RANK.get(cb, 0) <= _BAND_RANK.get(sb, 0):
            continue
        sk, ck = _BAND_COUNT_KEY.get(sb), _BAND_COUNT_KEY.get(cb)
        if sk and ck:
            stats[sk] = max(0, int(stats.get(sk, 0)) - 1)
            stats[ck] = int(stats.get(ck, 0)) + 1


async def _augment_timeseries_with_contract_anomaly(
    network: str, days: int, data: List[Dict[str, Any]],
) -> None:
    """Add flagged txs that are an alert (High/Critical) by their EFFECTIVE band
    but NOT by their stored band into the daily alert counts, bucketed on block
    date (matching the timeseries). Txs already alert-banded by their stored
    score are counted by the base query, so they are skipped to avoid double
    counting. Mutates ``data`` ([{date, count}], zero-filled). Best-effort."""
    bands = await _flagged_effective_bands(network)
    candidates = [
        tx for tx, (sb, cb) in bands.items()
        if cb in _ALERT_BANDS and sb not in _ALERT_BANDS
    ]
    if not candidates:
        return
    dates = await clickhouse.get_tx_block_dates_async(network, candidates, days)
    by_date: Dict[str, int] = {}
    for d in dates.values():
        by_date[d] = by_date.get(d, 0) + 1
    index = {row["date"]: row for row in data}
    for d, c in by_date.items():
        if d in index:  # block dates are within the window the query bounds
            index[d]["count"] += c


def _row_to_class_score(row: Dict[str, Any]) -> ClassScoreResult:
    scores = {name: float(row.get(name, -1)) for name in _CLASS_NAMES}
    def _decode_json_field(key: str) -> Dict[str, Any]:
        value = row.get(key, {})
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return {}
        return value or {}

    sub_scores = _decode_json_field("sub_scores")
    evidence = _decode_json_field("evidence")
    return ClassScoreResult(
        tx_hash=row["tx_hash"],
        network=row["network"],
        scores=scores,
        max_score=float(row["max_score"]),
        max_class=row["max_class"],
        risk_band=RiskBand(row["risk_band"]),
        sub_scores=sub_scores,
        evidence=evidence,
        analysis_version=row["analysis_version"],
        analyzed_at=row["analyzed_at"],
        corroboration_count=int(row.get("corroboration_count", 0) or 0),
        corroborating_classes=row.get("corroborating_classes", "") or "",
        fee=row.get("fee"),
        output_count=row.get("output_count"),
    )


@router.get("/results/{tx_hash}", dependencies=[Security(verify_api_key)])
async def get_analysis_result(tx_hash: str) -> ClassScoreResult:
    """Full 9-class score vector with sub-score drill-down for a single transaction.

    If the transaction has been admin-archived as a false positive, the score is
    still returned (for audit context) and the ``archived`` field is populated
    so the UI can render it differently.
    """
    try:
        row = await clickhouse.get_class_scores_async(tx_hash)
    except Exception as e:
        logger.error(f"Error fetching result for {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch result")
    if not row:
        raise HTTPException(status_code=404, detail=f"No result found for {tx_hash}")
    result = _row_to_class_score(row)
    try:
        archive_meta = await archive_queries.archive_get_async(
            row["network"], row["tx_hash"],
        )
        if archive_meta:
            result.archived = {
                "note": archive_meta["note"],
                "archived_by": archive_meta["archived_by"],
                "archived_at": format_iso_utc(archive_meta["archived_at"]),
                "source": archive_meta["source"],
            }
    except Exception as e:
        # Archive enrichment is best-effort; never fail the main fetch.
        logger.warning(f"Archive enrichment failed for {tx_hash}: {e}")
    if settings.CLUSTERING_ENABLED:
        try:
            ca = await clustering_queries.get_contract_anomaly_async(
                row["network"], row["tx_hash"],
            )
            if ca:
                _merge_contract_anomaly(result, ca)
        except Exception as e:
            # Read-time merge is best-effort; never fail the main fetch.
            logger.warning(f"contract_anomaly merge failed for {tx_hash}: {e}")
    return result


@router.get("/results", dependencies=[Security(verify_api_key)])
async def list_analysis_results(
    network: Optional[NetworkType] = Query(None),
    risk_band: List[RiskBand] = Query(
        default_factory=list,
        description=(
            "Filter by risk band. Repeat the param to OR-match multiple "
            "values, e.g. `?risk_band=Critical&risk_band=High`."
        ),
    ),
    attack_class: Optional[str] = Query(
        None, description="Filter by attack class name (e.g. phishing, sandwich)",
    ),
    min_score: float = Query(0.0, ge=0.0, le=100.0, description="Minimum score filter"),
    min_corroboration: int = Query(
        0, ge=0, le=len(_CLASS_NAMES),
        description=(
            "Only include transactions where at least this many distinct attack "
            "classes independently corroborated (scored above the corroboration "
            "threshold). 0 = no filter. Surfaces multi-signal transactions; does "
            "not change risk bands."
        ),
    ),
    sort: str = Query("score", description="Sort order: 'score' or 'date'"),
    analyzed_from: Optional[datetime] = Query(
        None,
        description="Only include results with analyzed_at >= this ISO timestamp (inclusive).",
    ),
    analyzed_to: Optional[datetime] = Query(
        None,
        description="Only include results with analyzed_at < this ISO timestamp (exclusive).",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """List multi-class scoring results with optional filters."""
    if attack_class and attack_class not in _CLASS_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown attack class '{attack_class}'. Valid: {_CLASS_NAMES}",
        )
    if sort not in ("score", "date"):
        raise HTTPException(status_code=400, detail="sort must be 'score' or 'date'")
    query_network = network or settings.CARDANO_NETWORK
    try:
        # Normalize the enum list to plain strings, passing None when the
        # caller didn't supply any band so the DB layer skips the WHERE.
        rbs = [b.value for b in risk_band] if risk_band else None
        # Shared filter predicate: list and count MUST apply identical filters or
        # the pagination total drifts from the rows shown. sort/limit/offset are
        # list-only (they do not affect the count) and stay out of this dict.
        filters = dict(
            network=query_network,
            risk_band=rbs,
            attack_class=attack_class,
            min_score=min_score,
            analyzed_from=analyzed_from,
            analyzed_to=analyzed_to,
            min_corroboration=min_corroboration,
        )
        rows = await clickhouse.get_class_scores_list_async(
            **filters, sort=sort, limit=limit, offset=offset,
        )
        total = await clickhouse.count_class_scores_async(**filters)
        data = [_row_to_class_score(r) for r in rows]
        if settings.CLUSTERING_ENABLED and data:
            # Batch-merge sidecar verdicts into the page, mirroring the
            # fee/output_count batch-fetch pattern. The merge only RAISES
            # score/band (recall-safe); it enriches each row's payload.
            # Best-effort: never fail the list.
            try:
                ca_by_hash = await clustering_queries.get_contract_anomaly_batch_async(
                    query_network, [d.tx_hash for d in data],
                )
                for d in data:
                    ca = ca_by_hash.get(d.tx_hash)
                    if ca:
                        _merge_contract_anomaly(d, ca)
            except Exception as e:
                logger.warning(f"contract_anomaly batch merge failed: {e}")
        # Recall rescue (recall-first, see CLAUDE.md): the score/band filter is
        # applied by the DB on the STORED 9-class score, before the merge above.
        # A tx whose stored score sits below the filter but whose contract_anomaly
        # verdict projects ABOVE it would be silently dropped from a filtered
        # page. Re-admit those flagged txs so a filtered triage view can never
        # hide a sidecar detection. Scoped to the first page (rescued rows ride
        # on top of it) and to score/band filters; attack_class and
        # min_corroboration are 9-class-specific (the synthetic class is neither
        # a max_class value nor counted in corroboration_count), so a rescue
        # under them would contradict the filter's intent.
        rescued_total = 0
        # Recall surface (recall-first, see CLAUDE.md): the DB filters AND orders on
        # the STORED 9-class score, before the contract_anomaly merge. A tx whose
        # stored score sits below an active filter (or just below page 1 in an
        # unfiltered list) but whose sidecar verdict projects ABOVE it would be
        # hidden from a filtered page or buried at the bottom of an unfiltered one.
        # Surface those flagged txs on the first page. Skipped under attack_class /
        # min_corroboration: the synthetic class is neither a max_class value nor
        # counted in corroboration_count, so a rescue there contradicts the filter.
        rescue_active = (
            settings.CLUSTERING_ENABLED
            and offset == 0
            and not attack_class
            and min_corroboration == 0
        )
        n_db_rows = len(data)
        if rescue_active:
            try:
                flagged = await clustering_queries.flagged_for_network_async(
                    query_network,
                )
                if len(flagged) >= clustering_queries._RESCUE_FETCH_CAP:
                    # No silent caps: a truncated rescue set could omit a flagged tx
                    # from page 1; surface it so the cap can be raised.
                    logger.warning(
                        "contract_anomaly rescue hit the fetch cap (%d) for %s; "
                        "older flagged txs may be absent from the first page",
                        clustering_queries._RESCUE_FETCH_CAP, query_network,
                    )
                present = {d.tx_hash for d in data}
                rescue_hashes = [h for h in flagged if h not in present]
                filtered = min_score > 0 or bool(rbs)
                # Unfiltered: surface a buried tx only if its effective score would
                # rank it within page 1. Once the page is full, that means beating
                # the lowest stored score shown; while it has room, any flagged tx
                # qualifies.
                page_floor = (
                    min((d.max_score for d in data), default=0.0)
                    if len(data) >= limit else 0.0
                )
                if rescue_hashes:
                    rescue_rows = await clickhouse.get_class_scores_by_hashes_async(
                        query_network, rescue_hashes,
                    )
                    for r in rescue_rows:
                        res = _row_to_class_score(r)
                        if not _within_analyzed_window(
                            res.analyzed_at, analyzed_from, analyzed_to,
                        ):
                            continue
                        stored_meets = _passes_score_band(
                            res.max_score, res.risk_band, min_score, rbs,
                        )
                        _merge_contract_anomaly(res, flagged[res.tx_hash])
                        if filtered:
                            # Genuinely rescued only: stored score missed the filter
                            # but the merged score now meets it. A row whose stored
                            # score already met the filter is in the normal paginated
                            # set, so it must not be added to total here.
                            if not stored_meets and _passes_score_band(
                                res.max_score, res.risk_band, min_score, rbs,
                            ):
                                data.append(res)
                                rescued_total += 1
                        elif res.max_score >= page_floor:
                            # Unfiltered: the tx is already counted in `total` (its
                            # stored row exists), so don't bump it; it surfaces on
                            # page 1 by effective rank and may also appear on a later
                            # stored-ordered page (recall-first favours visibility).
                            data.append(res)
            except Exception as e:
                logger.warning(f"contract_anomaly rescue failed: {e}")
        # Re-sort so surfaced/rescued rows interleave by effective rank rather than
        # trailing the page; matches the SQL ORDER BY (score: max_score then recency).
        if len(data) > n_db_rows:
            if sort == "date":
                data.sort(key=lambda d: (d.analyzed_at, d.max_score), reverse=True)
            else:
                data.sort(key=lambda d: (d.max_score, d.analyzed_at), reverse=True)
        return {
            "count": len(data),
            "total": total + rescued_total,
            "data": data,
        }
    except Exception as e:
        logger.error(f"Error listing results: {e}")
        raise HTTPException(status_code=500, detail="Failed to list results")


@router.get("/stats", dependencies=[Security(verify_api_key)])
async def analysis_stats(
    network: Optional[NetworkType] = Query(None),
):
    """Per-class score distributions, band counts, and aggregate stats."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        stats = await clickhouse.get_class_scores_stats_async(query_network)
        if settings.CLUSTERING_ENABLED:
            # Reconcile band counts to the EFFECTIVE band so contract-anomaly-only
            # detections aren't undercounted in the KPI cards. Best-effort: the
            # sidecar being down must not fail the dashboard's stats.
            try:
                await _augment_stats_with_contract_anomaly(query_network, stats)
            except Exception as e:
                logger.warning(f"contract_anomaly stats augmentation failed: {e}")
        return stats
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch stats")


@router.get("/stats/timeseries", dependencies=[Security(verify_api_key)])
async def analysis_stats_timeseries(
    network: Optional[NetworkType] = Query(None),
    days: int = Query(14, ge=1, le=90, description="Trailing window in days"),
):
    """Daily High+Critical alert counts over a trailing window, bucketed on
    on-chain block time. Powers the dashboard sparkline. Returns a list of
    ``{date, count}`` with zero-filled gaps, oldest first."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        data = await clickhouse.get_alert_timeseries_async(query_network, days)
        if settings.CLUSTERING_ENABLED:
            # Fold contract-anomaly-only alerts (High/Critical by effective band)
            # into the daily counts so the sparkline matches the KPI cards.
            # Best-effort: never fail the timeseries on a sidecar hiccup.
            try:
                await _augment_timeseries_with_contract_anomaly(
                    query_network, days, data,
                )
            except Exception as e:
                logger.warning(f"contract_anomaly timeseries augmentation failed: {e}")
        return {"network": query_network, "days": days, "data": data}
    except Exception as e:
        logger.error(f"Error fetching timeseries: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch timeseries")


@router.get("/baselines/{scope_type}/{scope_id}", dependencies=[Security(verify_api_key)])
async def get_baselines(
    scope_type: str,
    scope_id: str,
    network: Optional[NetworkType] = Query(None),
):
    """Inspect baseline percentiles for a given scope (e.g. per_script, global)."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        rows = await clickhouse.get_baselines_for_scope_async(
            query_network, scope_type, scope_id,
        )
        return {
            "network": query_network,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "baselines": rows,
        }
    except Exception as e:
        logger.error(f"Error fetching baselines: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch baselines")
