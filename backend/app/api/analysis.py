"""API endpoints for Analysis Engine results"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Security

from app.auth import verify_api_key
from app.db import clickhouse
from app.models.transaction import AnalysisStats, RiskLevel, TransactionAnalysisResult

NetworkType = Literal["mainnet", "preprod"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


def _row_to_result(row: dict) -> TransactionAnalysisResult:
    return TransactionAnalysisResult(
        tx_hash=row["tx_hash"],
        network=row["network"],
        risk_score=float(row["risk_score"]),
        risk_level=RiskLevel(row["risk_level"]),
        cluster_id=int(row["cluster_id"]),
        is_anomaly=bool(row["is_anomaly"]),
        anomaly_reasons=list(row["anomaly_reasons"]),
        analysis_version=row["analysis_version"],
        analyzed_at=row["analyzed_at"],
    )


@router.get("/stats", dependencies=[Security(verify_api_key)])
async def analysis_stats(
    network: NetworkType = Query("preprod"),
) -> AnalysisStats:
    """Aggregate Analysis Engine statistics: total scored, risk distribution, anomaly count."""
    try:
        raw = await clickhouse.get_analysis_stats_async(network)
        return AnalysisStats(
            total_analyzed=int(raw["total_analyzed"]),
            avg_risk_score=float(raw["avg_risk_score"]) if raw["avg_risk_score"] is not None else None,
            high_risk_count=int(raw["high_risk_count"]),
            anomaly_count=int(raw["anomaly_count"]),
            cluster_count=int(raw["cluster_count"]),
            last_run_at=raw["last_run_at"],
        )
    except Exception as e:
        logger.error(f"Error fetching analysis stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch analysis stats")


@router.get("/results/{tx_hash}", dependencies=[Security(verify_api_key)])
async def get_analysis_result(tx_hash: str) -> TransactionAnalysisResult:
    """Get the Analysis Engine result for a specific transaction."""
    try:
        row = await clickhouse.get_analysis_result_async(tx_hash)
    except Exception as e:
        logger.error(f"Error fetching analysis result for {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch analysis result")
    if not row:
        raise HTTPException(status_code=404, detail=f"No analysis result found for {tx_hash}")
    return _row_to_result(row)


@router.get("/results", dependencies=[Security(verify_api_key)])
async def list_analysis_results(
    network: NetworkType = Query("preprod"),
    risk_level: Optional[RiskLevel] = Query(
        None,
        description="Filter by risk level: LOW, MEDIUM, HIGH. Returns all levels if omitted.",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """List Analysis Engine results, optionally filtered by risk level."""
    try:
        rows = await clickhouse.get_analysis_results_async(
            network=network,
            risk_level=risk_level.value if risk_level else None,
            limit=limit,
            offset=offset,
        )
        return {"count": len(rows), "data": [_row_to_result(r) for r in rows]}
    except Exception as e:
        logger.error(f"Error listing analysis results: {e}")
        raise HTTPException(status_code=500, detail="Failed to list analysis results")
