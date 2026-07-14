"""API endpoints for the multi-class Analysis Engine"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Security
from pydantic import BaseModel

from app.analysis.engine import _CLASS_NAMES
from app.api._params import NetworkParam, PageLimit, PageOffset, TimeFromParam, TimeToParam
from app.api.contract_anomaly_read import (
    _CONTRACT_ANOMALY,
    _augment_stats_with_contract_anomaly,
    _augment_timeseries_with_contract_anomaly,
    _list_contract_anomaly_results,
    _merge_contract_anomaly,
    _merge_overlay_onto_page,
    _rescue_flagged_onto_page,
    _row_to_class_score,
)
from app.auth import verify_api_key
from app.config import settings
from app.db import archive_queries, clickhouse, clustering_queries
from app.models.common import ListResponse
from app.models.transaction import ClassScoreResult, RiskBand
from app.utils.datetime_utils import format_iso_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])

# _CLASS_NAMES is imported from app.analysis.engine (the canonical scorer-order
# source) so the API's class validation can't drift from the engine's order.

# Attack classes the list filter accepts. The nine stored classes are filterable
# by the SQL path (max_class = attack_class); contract_anomaly is a read-time
# overlay with no DB column, so it is filtered in Python (see
# _list_contract_anomaly_results). It stays out of _CLASS_NAMES so the engine's
# scorer-order contract is unaffected.
_VALID_ATTACK_CLASSES = (*_CLASS_NAMES, _CONTRACT_ANOMALY)


@router.get("/results/{tx_hash}", dependencies=[Security(verify_api_key)])
async def get_analysis_result(
    tx_hash: str,
    network: NetworkParam = None,
) -> ClassScoreResult:
    """Full 9-class score vector with sub-score drill-down for a single transaction.

    ``network`` defaults to the configured network; it scopes the lookup so a
    tx_hash that also exists on another network cannot return the wrong row.

    If the transaction has been admin-archived as a false positive, the score is
    still returned (for audit context) and the ``archived`` field is populated
    so the UI can render it differently.
    """
    query_network = network or settings.CARDANO_NETWORK
    try:
        row = await clickhouse.get_class_scores_async(tx_hash, query_network)
    except Exception as e:
        logger.error(f"Error fetching result for {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch result")
    if not row:
        raise HTTPException(status_code=404, detail=f"No result found for {tx_hash}")
    result = _row_to_class_score(row)
    try:
        archive_meta = await archive_queries.archive_get_async(
            row["network"],
            row["tx_hash"],
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
                row["network"],
                row["tx_hash"],
            )
            if ca:
                _merge_contract_anomaly(result, ca)
        except Exception as e:
            # Read-time merge is best-effort; never fail the main fetch.
            logger.warning(f"contract_anomaly merge failed for {tx_hash}: {e}")
    return result


@router.get(
    "/results",
    dependencies=[Security(verify_api_key)],
    response_model=ListResponse[ClassScoreResult],
)
async def list_analysis_results(
    network: NetworkParam = None,
    risk_band: list[RiskBand] = Query(
        default_factory=list,
        description=(
            "Filter by risk band. Repeat the param to OR-match multiple "
            "values, e.g. `?risk_band=Critical&risk_band=High`."
        ),
    ),
    attack_class: str | None = Query(
        None,
        description="Filter by attack class name (e.g. phishing, sandwich)",
    ),
    min_score: float = Query(0.0, ge=0.0, le=100.0, description="Minimum score filter"),
    min_corroboration: int = Query(
        0,
        ge=0,
        le=len(_CLASS_NAMES),
        description=(
            "Only include transactions where at least this many distinct attack "
            "classes independently corroborated (scored above the corroboration "
            "threshold). 0 = no filter. Surfaces multi-signal transactions; does "
            "not change risk bands."
        ),
    ),
    sort: str = Query("score", description="Sort order: 'score' or 'date'"),
    analyzed_from: TimeFromParam = None,
    analyzed_to: TimeToParam = None,
    limit: PageLimit = 100,
    offset: PageOffset = 0,
):
    """List multi-class scoring results with optional filters.

    The ``from``/``to`` window filters on ``analyzed_at`` with the shared
    half-open [from, to) convention (see ``app.api._params``).
    """
    if attack_class and attack_class not in _VALID_ATTACK_CLASSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown attack class '{attack_class}'. Valid: {list(_VALID_ATTACK_CLASSES)}",
        )
    if sort not in ("score", "date"):
        raise HTTPException(status_code=422, detail="sort must be 'score' or 'date'")
    query_network = network or settings.CARDANO_NETWORK
    try:
        # Normalize the enum list to plain strings, passing None when the
        # caller didn't supply any band so the DB layer skips the WHERE.
        rbs = [b.value for b in risk_band] if risk_band else None
        # The synthetic class has no DB column, so the SQL path can't filter it.
        # Route it to the in-memory resolver. When clustering is disabled the
        # class never exists, so the filtered page is legitimately empty (not an
        # error): the frontend offers the filter unconditionally.
        if attack_class == _CONTRACT_ANOMALY:
            if not settings.CLUSTERING_ENABLED:
                return {"count": 0, "total": 0, "data": []}
            try:
                ca_data, ca_total = await _list_contract_anomaly_results(
                    query_network,
                    bands=rbs,
                    min_score=min_score,
                    analyzed_from=analyzed_from,
                    analyzed_to=analyzed_to,
                    min_corroboration=min_corroboration,
                    sort=sort,
                    limit=limit,
                    offset=offset,
                )
            except Exception:
                # Degrade to an empty page rather than fail the request (matching
                # the sidecar read path). But log at ERROR WITH the traceback: the
                # clustering reads already swallow a sidecar hiccup upstream
                # (returning {}), so anything reaching here is an UNEXPECTED
                # in-process error, not a routine outage. Logging it at WARNING is
                # what let a naive/aware TypeError masquerade as "no anomalies" —
                # a silent recall loss. ERROR + exc_info makes the next such bug
                # loud instead of an invisible empty page.
                logger.error(
                    "contract_anomaly list filter: unexpected error, returning empty page",
                    exc_info=True,
                )
                ca_data, ca_total = [], 0
            return {"count": len(ca_data), "total": ca_total, "data": ca_data}
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
            **filters,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        total = await clickhouse.count_class_scores_async(**filters)
        data = [_row_to_class_score(r) for r in rows]
        # Enrich the page with sidecar verdicts, then re-admit any flagged tx the
        # DB filter dropped on its stored score (recall rescue). Both are
        # recall-safe and best-effort; see the helper docstrings.
        await _merge_overlay_onto_page(query_network, data)
        rescued_total = await _rescue_flagged_onto_page(
            query_network,
            data,
            min_score=min_score,
            bands=rbs,
            attack_class=attack_class,
            min_corroboration=min_corroboration,
            analyzed_from=analyzed_from,
            analyzed_to=analyzed_to,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        return {
            "count": len(data),
            "total": total + rescued_total,
            "data": data,
        }
    except Exception as e:
        logger.error(f"Error listing results: {e}")
        raise HTTPException(status_code=500, detail="Failed to list results")


class PerClassStats(BaseModel):
    scored_count: int
    avg_score: float | None
    max_score: float | None


class AnalysisStatsOut(BaseModel):
    total: int
    critical_count: int
    high_count: int
    moderate_count: int
    informational_count: int
    avg_max_score: float | None
    last_analyzed_at: datetime | None
    per_class: dict[str, PerClassStats]
    pending_count: int


@router.get("/stats", dependencies=[Security(verify_api_key)], response_model=AnalysisStatsOut)
async def analysis_stats(
    network: NetworkParam = None,
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
    network: NetworkParam = None,
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
                    query_network,
                    days,
                    data,
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
    network: NetworkParam = None,
):
    """Inspect baseline percentiles for a given scope (e.g. per_script, global)."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        rows = await clickhouse.get_baselines_for_scope_async(
            query_network,
            scope_type,
            scope_id,
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
