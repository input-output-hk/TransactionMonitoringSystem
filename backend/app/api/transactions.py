"""API endpoints for querying transactions from ClickHouse"""

import json
import logging
import re
from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import APIRouter, Query, HTTPException, Security
from pydantic import BaseModel

from app.db import clickhouse
from app.config import settings
from app.auth import verify_api_key
from app.api._params import NetworkParam

logger = logging.getLogger(__name__)

# Cardano tx hash: exactly 64 lowercase hex characters.
_TX_HASH_RE = re.compile(r'^[0-9a-f]{64}$')
# Cardano addresses: bech32 (addr1.../addr_test1...) and legacy base58 (Ae2.../Dzz...).
# Only alphanumeric + underscore; no SQL metacharacters possible.
_ADDRESS_RE = re.compile(r'^[A-Za-z0-9_]{10,200}$')

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


class TransactionResponse(BaseModel):
    """Transaction response model"""
    tx_hash: str
    slot: Optional[int]
    block_height: Optional[int]
    block_hash: Optional[str]
    block_index: Optional[int]
    timestamp: datetime
    fee: int
    deposit: Optional[int]
    input_count: int
    output_count: int
    total_input_value: Optional[int]
    total_output_value: int
    addresses: List[str]


class TransactionDetailResponse(TransactionResponse):
    """Detailed transaction response with inputs and outputs"""
    inputs: List[Dict[str, Any]]
    outputs: List[Dict[str, Any]]
    metadata: Optional[Dict[str, Any]] = None


def _row_to_transaction(row: Any) -> TransactionResponse:
    """Map a positional transactions-table row onto a TransactionResponse.

    Single-sourced so the list and detail handlers share one field-to-index
    contract: a reordered SELECT column would otherwise silently misalign one
    handler with no type error. The detail handler reuses this via ``model_dump()``
    and adds inputs/outputs/metadata.
    """
    return TransactionResponse(
        tx_hash=row[0],
        slot=row[1],
        block_height=row[2],
        block_hash=row[3],
        block_index=row[4],
        timestamp=row[5],
        fee=row[6],
        deposit=row[7],
        input_count=row[8],
        output_count=row[9],
        total_input_value=row[10],
        total_output_value=row[11],
        addresses=row[12] if row[12] else [],
    )


@router.get("/", response_model=List[TransactionResponse])
async def get_transactions(
    network: NetworkParam = None,
    limit: int = Query(100, ge=1, le=200, description="Maximum number of transactions to return"),
    before: Optional[datetime] = Query(
        None,
        description="Cursor pagination: return transactions strictly before this timestamp (ISO format).",
    ),
    address: Optional[str] = Query(None, description="Filter by address (any input or output)"),
    api_key: str = Security(verify_api_key),
):
    """List transactions from ClickHouse."""
    if address and not _ADDRESS_RE.match(address):
        raise HTTPException(status_code=422, detail="Invalid address format")
    try:
        query_network = network or settings.CARDANO_NETWORK
        params: Dict[str, Any] = {"network": query_network, "limit": limit}

        if address:
            before_clause = ""
            if before:
                before_clause = "AND t.timestamp < %(before)s"
                params["before"] = before
            params["address"] = address
            query = f"""
                SELECT
                    t.tx_hash, t.slot, t.block_height, t.block_hash, t.block_index,
                    t.timestamp, t.fee, t.deposit,
                    t.input_count, t.output_count, t.total_input_value, t.total_output_value,
                    t.addresses
                FROM transactions t
                INNER JOIN (
                    SELECT DISTINCT tx_hash
                    FROM address_transactions
                    WHERE network = %(network)s
                      AND address = %(address)s
                ) at USING tx_hash
                WHERE t.network = %(network)s
                  {before_clause}
                ORDER BY t.timestamp DESC
                LIMIT %(limit)s
            """
        else:
            before_clause = ""
            if before:
                before_clause = "AND timestamp < %(before)s"
                params["before"] = before
            query = f"""
                SELECT
                    tx_hash, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
                    input_count, output_count, total_input_value, total_output_value, addresses
                FROM transactions
                WHERE network = %(network)s
                  {before_clause}
                ORDER BY timestamp DESC
                LIMIT %(limit)s
            """

        results = await clickhouse.execute_query_async(query, params)

        transactions = [_row_to_transaction(row) for row in results]
        return transactions

    except Exception as e:
        logger.error(f"Error querying transactions: {e}")
        raise HTTPException(status_code=500, detail="Failed to query transactions")


@router.get("/{tx_hash}", response_model=TransactionDetailResponse)
async def get_transaction_by_hash(
    tx_hash: str,
    network: NetworkParam = None,
    api_key: str = Security(verify_api_key),
):
    """Get detailed transaction information by hash"""
    if not _TX_HASH_RE.match(tx_hash):
        raise HTTPException(status_code=422, detail="Invalid transaction hash: must be 64 lowercase hex characters")
    try:
        query_network = network or settings.CARDANO_NETWORK
        params = {"tx_hash": tx_hash, "network": query_network}

        tx_results = await clickhouse.execute_query_async("""
            SELECT
                tx_hash, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
                input_count, output_count, total_input_value, total_output_value, addresses, metadata
            FROM transactions
            WHERE tx_hash = %(tx_hash)s AND network = %(network)s
            LIMIT 1
        """, params)

        if not tx_results:
            raise HTTPException(status_code=404, detail="Transaction not found")

        tx_row = tx_results[0]

        inputs_results = await clickhouse.execute_query_async("""
            SELECT
                input_tx_hash, input_index_in_tx, address, amount, assets,
                is_reference, is_collateral, is_unspent_attempt
            FROM transaction_inputs
            WHERE tx_hash = %(tx_hash)s AND network = %(network)s
            ORDER BY input_index
            LIMIT 500
        """, params)

        inputs = []
        for row in inputs_results:
            assets = None
            if row[4]:
                try:
                    assets = json.loads(row[4])
                except Exception:
                    assets = {"raw": row[4]}
            inputs.append({
                "tx_hash": row[0],
                "index": row[1],
                "address": row[2],
                "amount": row[3],
                "assets": assets,
                "is_reference": bool(row[5]),
                "is_collateral": bool(row[6]),
                # Failed-tx attempted spend: shown in the detail view (what
                # the tx TRIED to consume), excluded from flow analytics.
                "is_unspent_attempt": bool(row[7]),
            })

        outputs_results = await clickhouse.execute_query_async("""
            SELECT
                output_index, address, amount, assets, is_collateral
            FROM transaction_outputs
            WHERE tx_hash = %(tx_hash)s AND network = %(network)s
            ORDER BY output_index
            LIMIT 500
        """, params)

        outputs = []
        for row in outputs_results:
            assets = None
            if row[3]:
                try:
                    assets = json.loads(row[3])
                except Exception:
                    assets = {"raw": row[3]}
            outputs.append({
                "index": row[0],
                "address": row[1],
                "amount": row[2],
                "assets": assets,
                "is_collateral": bool(row[4])
            })

        metadata = None
        if tx_row[13]:
            try:
                metadata = json.loads(tx_row[13])
            except Exception:
                metadata = {"raw": tx_row[13]}

        return TransactionDetailResponse(
            **_row_to_transaction(tx_row).model_dump(),
            inputs=inputs,
            outputs=outputs,
            metadata=metadata,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error querying transaction {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to query transaction")


@router.get("/address/{address}", response_model=List[TransactionResponse])
async def get_transactions_by_address(
    address: str,
    network: NetworkParam = None,
    limit: int = Query(100, ge=1, le=200),
    before: Optional[datetime] = Query(
        None,
        description="Cursor pagination: return transactions strictly before this timestamp (ISO format).",
    ),
    api_key: str = Security(verify_api_key),
):
    """Get all transactions involving a specific address"""
    return await get_transactions(network=network, address=address, limit=limit, before=before, api_key=api_key)


@router.get("/blocks/recent")
async def get_recent_blocks(
    network: NetworkParam = None,
    limit: int = Query(5, ge=1, le=50),
    api_key: str = Security(verify_api_key),
):
    """Recent blocks aggregated from the transactions table.

    The schema has no dedicated ``blocks`` table, so we derive blocks by
    grouping transactions on ``(block_height, block_hash)``. Consequence:
    empty blocks (zero txs) never appear here. For a "Latest Blocks"
    dashboard widget that's the desired behavior anyway — empty blocks
    aren't interesting.

    Note: this is a multi-segment path (`/blocks/recent`), so it doesn't
    collide with `GET /{tx_hash}` regardless of registration order.
    """
    query_network = network or settings.CARDANO_NETWORK
    try:
        rows = await clickhouse.execute_query_async(
            """
            SELECT
                block_height,
                block_hash,
                min(timestamp) AS timestamp,
                count() AS tx_count,
                sum(total_output_value) AS total_output_value
            FROM transactions
            WHERE network = %(network)s AND block_height IS NOT NULL
            GROUP BY block_height, block_hash
            -- block_height tie-breaker: timestamp is ingestion wall-clock
            -- time with 1s granularity, so catch-up replay lands many
            -- blocks on the same value and ties returned in random order.
            ORDER BY timestamp DESC, block_height DESC
            LIMIT %(limit)s
            """,
            {"network": query_network, "limit": limit},
        )
    except Exception as e:
        logger.error(f"Error fetching recent blocks: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch recent blocks")
    return [
        {
            "block_height": r[0],
            "block_hash": r[1],
            "timestamp": r[2].isoformat() if r[2] else None,
            "tx_count": r[3],
            "total_output_value": r[4],
        }
        for r in rows
    ]


@router.get("/stats/summary")
async def get_transaction_stats(
    network: NetworkParam = None,
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    api_key: str = Security(verify_api_key),
):
    """Get transaction statistics"""
    try:
        query_network = network or settings.CARDANO_NETWORK
        params: Dict[str, Any] = {"network": query_network}

        time_clauses = ""
        if start_time:
            time_clauses += " AND timestamp >= %(start_time)s"
            params["start_time"] = start_time
        if end_time:
            time_clauses += " AND timestamp <= %(end_time)s"
            params["end_time"] = end_time

        results = await clickhouse.execute_query_async(f"""
            SELECT
                count() as total_count,
                sum(total_output_value) as total_volume,
                sum(fee) as total_fees,
                avg(total_output_value) as avg_value,
                min(timestamp) as first_tx,
                max(timestamp) as last_tx
            FROM transactions
            WHERE network = %(network)s
              {time_clauses}
        """, params)
        row = results[0]

        return {
            "total_count": row[0],
            "total_volume": row[1],
            "total_fees": row[2],
            "avg_value": row[3],
            "first_tx": row[4].isoformat() if row[4] else None,
            "last_tx": row[5].isoformat() if row[5] else None
        }

    except Exception as e:
        logger.error(f"Error getting transaction stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get transaction stats")


@router.get("/stats/throughput")
async def get_transaction_throughput(
    network: NetworkParam = None,
    window_minutes: int = Query(
        5, ge=1, le=1440,
        description="Sliding window size in minutes (default 5).",
    ),
    api_key: str = Security(verify_api_key),
):
    """Recent transaction throughput.

    Counts transactions ingested in the last ``window_minutes`` and returns
    the implied ``tx/min`` rate. The dashboard's "TX / min" KPI uses this
    instead of a lifetime average so the value reflects current pipeline
    activity, not a denominator that grows forever.

    ``subtractMinutes(now(), N)`` lets clickhouse-driver substitute the
    window safely as a numeric parameter.
    """
    try:
        query_network = network or settings.CARDANO_NETWORK
        results = await clickhouse.execute_query_async(
            """
            SELECT count() AS recent_count
            FROM transactions
            WHERE network = %(network)s
              AND timestamp >= subtractMinutes(now(), %(window_minutes)s)
            """,
            {"network": query_network, "window_minutes": window_minutes},
        )
        count = int(results[0][0]) if results else 0
        return {
            "window_minutes": window_minutes,
            "count": count,
            "tx_per_min": count / window_minutes,
        }
    except Exception as e:
        logger.error(f"Error getting transaction throughput: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to get transaction throughput",
        )
