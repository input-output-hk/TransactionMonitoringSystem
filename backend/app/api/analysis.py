"""API endpoints for the multi-class Analysis Engine"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Security

from app.analysis import contract_anomaly as ca_projection
from app.analysis.contract_anomaly import corroboration_threshold
from app.analysis.normalise import score_to_band
from app.auth import verify_api_key
from app.config import settings
from app.db import archive_queries, clickhouse, clustering_queries
from app.models.transaction import ClassScoreResult, NetworkType, RiskBand
from app.utils.datetime_utils import format_iso_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

_CLASS_NAMES = [
    "token_dust", "large_value", "large_datum", "multiple_sat",
    "front_running", "sandwich", "circular", "fake_token", "phishing",
]

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
        rows = await clickhouse.get_class_scores_list_async(
            network=query_network,
            risk_band=rbs,
            attack_class=attack_class,
            min_score=min_score,
            sort=sort,
            analyzed_from=analyzed_from,
            analyzed_to=analyzed_to,
            limit=limit,
            offset=offset,
            min_corroboration=min_corroboration,
        )
        total = await clickhouse.count_class_scores_async(
            network=query_network,
            risk_band=rbs,
            attack_class=attack_class,
            min_score=min_score,
            analyzed_from=analyzed_from,
            analyzed_to=analyzed_to,
            min_corroboration=min_corroboration,
        )
        data = [_row_to_class_score(r) for r in rows]
        if settings.CLUSTERING_ENABLED and data:
            # Batch-merge sidecar verdicts into the page, mirroring the
            # fee/output_count batch-fetch pattern. Server-side filter/sort on
            # the synthetic class is intentionally out of scope for now (the
            # page is still ordered by the stored max_score); the merge only
            # enriches each row's payload. Best-effort: never fail the list.
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
        return {
            "count": len(rows),
            "total": total,
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
        return await clickhouse.get_class_scores_stats_async(query_network)
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
