"""API endpoints for transaction lifecycle queries"""

import logging
from typing import Optional
from fastapi import APIRouter, Query, HTTPException, Security

from app.api._params import NetworkParam
from app.config import settings
from app.db import postgres
from app.auth import verify_api_key
from app.models.transaction import (
    TransactionLifecycleEvent,
    LifecycleSummaryStats,
    LifecycleStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])


@router.get("/stats/summary", dependencies=[Security(verify_api_key)])
async def lifecycle_stats(
    network: NetworkParam = None,
) -> LifecycleSummaryStats:
    """Aggregate lifecycle statistics: pending count, avg latency, rollback rate."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        stats = await postgres.get_lifecycle_summary(query_network)
        return LifecycleSummaryStats(
            total_tracked=stats["total_tracked"],
            pending_count=stats["pending_count"],
            confirmed_count=stats["confirmed_count"],
            rolled_back_count=stats["rolled_back_count"],
            dropped_count=stats["dropped_count"],
            avg_latency_ms=float(stats["avg_latency_ms"]) if stats["avg_latency_ms"] else None,
            rollback_rate=float(stats["rollback_rate"]) if stats["rollback_rate"] else None,
        )
    except Exception as e:
        logger.error(f"Error getting lifecycle stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get lifecycle stats")


@router.get("/{tx_id}", dependencies=[Security(verify_api_key)])
async def get_lifecycle(tx_id: str) -> TransactionLifecycleEvent:
    """Get lifecycle state for a specific transaction."""
    row = await postgres.get_lifecycle_by_tx_id(tx_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Transaction {tx_id} not found")
    return TransactionLifecycleEvent(**row)


@router.get("", dependencies=[Security(verify_api_key)])
async def list_lifecycles(
    status: Optional[LifecycleStatus] = Query(
        None,
        description=(
            "Filter by status: PENDING, CONFIRMED, ROLLED_BACK, DROPPED. "
            "DROPPED transactions were PENDING but not confirmed within the "
            "LIFECYCLE_PENDING_TTL_SECONDS window. Returns all statuses if omitted."
        ),
    ),
    network: NetworkParam = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Query lifecycle records, optionally filtered by status. Returns all records when status is omitted."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        if status:
            rows = await postgres.get_lifecycles_by_status(
                status=status.value, network=query_network, limit=limit, offset=offset
            )
        else:
            rows = await postgres.get_all_lifecycles(
                network=query_network, limit=limit, offset=offset
            )
        return {"count": len(rows), "data": rows}
    except Exception as e:
        logger.error(f"Error querying lifecycles: {e}")
        raise HTTPException(status_code=500, detail="Failed to query lifecycles")
