"""API endpoints for the multi-class Analysis Engine"""

import json
import logging
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Security

from app.auth import verify_api_key
from app.db import clickhouse
from app.models.transaction import ClassScoreResult, RiskBand

NetworkType = Literal["mainnet", "preprod"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

_CLASS_NAMES = [
    "token_dust", "large_value", "large_datum", "multiple_sat",
    "front_running", "sandwich", "circular", "fake_token", "phishing",
]


def _row_to_class_score(row: Dict[str, Any]) -> ClassScoreResult:
    scores = {name: float(row.get(name, -1)) for name in _CLASS_NAMES}
    sub_scores = row.get("sub_scores", {})
    if isinstance(sub_scores, str):
        try:
            sub_scores = json.loads(sub_scores)
        except (json.JSONDecodeError, TypeError):
            sub_scores = {}
    return ClassScoreResult(
        tx_hash=row["tx_hash"],
        network=row["network"],
        scores=scores,
        max_score=float(row["max_score"]),
        max_class=row["max_class"],
        risk_band=RiskBand(row["risk_band"]),
        sub_scores=sub_scores,
        analysis_version=row["analysis_version"],
        analyzed_at=row["analyzed_at"],
        fee=row.get("fee"),
        output_count=row.get("output_count"),
    )


@router.get("/results/{tx_hash}", dependencies=[Security(verify_api_key)])
async def get_analysis_result(tx_hash: str) -> ClassScoreResult:
    """Full 9-class score vector with sub-score drill-down for a single transaction."""
    try:
        row = await clickhouse.get_class_scores_async(tx_hash)
    except Exception as e:
        logger.error(f"Error fetching result for {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch result")
    if not row:
        raise HTTPException(status_code=404, detail=f"No result found for {tx_hash}")
    return _row_to_class_score(row)


@router.get("/results", dependencies=[Security(verify_api_key)])
async def list_analysis_results(
    network: NetworkType = Query("preprod"),
    risk_band: Optional[RiskBand] = Query(None, description="Filter by risk band"),
    attack_class: Optional[str] = Query(
        None, description="Filter by attack class name (e.g. phishing, sandwich)",
    ),
    min_score: float = Query(0.0, ge=0.0, le=100.0, description="Minimum score filter"),
    sort: str = Query("score", description="Sort order: 'score' or 'date'"),
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
    try:
        rows = await clickhouse.get_class_scores_list_async(
            network=network,
            risk_band=risk_band.value if risk_band else None,
            attack_class=attack_class,
            min_score=min_score,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        return {"count": len(rows), "data": [_row_to_class_score(r) for r in rows]}
    except Exception as e:
        logger.error(f"Error listing results: {e}")
        raise HTTPException(status_code=500, detail="Failed to list results")


@router.get("/stats", dependencies=[Security(verify_api_key)])
async def analysis_stats(
    network: NetworkType = Query("preprod"),
):
    """Per-class score distributions, band counts, and aggregate stats."""
    try:
        return await clickhouse.get_class_scores_stats_async(network)
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch stats")


@router.get("/baselines/{scope_type}/{scope_id}", dependencies=[Security(verify_api_key)])
async def get_baselines(scope_type: str, scope_id: str):
    """Inspect baseline percentiles for a given scope (e.g. per_script, global)."""
    try:
        rows = await clickhouse.get_baselines_for_scope_async(scope_type, scope_id)
        return {"scope_type": scope_type, "scope_id": scope_id, "baselines": rows}
    except Exception as e:
        logger.error(f"Error fetching baselines: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch baselines")
