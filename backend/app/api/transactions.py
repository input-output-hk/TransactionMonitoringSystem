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
from app.models.transaction import NetworkType

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


@router.get("/", response_model=List[TransactionResponse])
async def get_transactions(
    network: Optional[NetworkType] = Query(
        None,
        description="Network to query: 'mainnet', 'preprod', or 'preview'. Defaults to the instance's CARDANO_NETWORK setting."
    ),
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

        transactions = []
        for row in results:
            transactions.append(TransactionResponse(
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
                addresses=row[12] if row[12] else []
            ))

        return transactions

    except Exception as e:
        logger.error(f"Error querying transactions: {e}")
        raise HTTPException(status_code=500, detail="Failed to query transactions")


@router.get("/{tx_hash}", response_model=TransactionDetailResponse)
async def get_transaction_by_hash(
    tx_hash: str,
    network: Optional[NetworkType] = Query(
        None,
        description="Network to query: 'mainnet', 'preprod', or 'preview'. Defaults to the instance's CARDANO_NETWORK setting."
    ),
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
                input_tx_hash, input_index_in_tx, address, amount, assets, is_reference, is_collateral
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
                "is_collateral": bool(row[6])
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
            tx_hash=tx_row[0],
            slot=tx_row[1],
            block_height=tx_row[2],
            block_hash=tx_row[3],
            block_index=tx_row[4],
            timestamp=tx_row[5],
            fee=tx_row[6],
            deposit=tx_row[7],
            input_count=tx_row[8],
            output_count=tx_row[9],
            total_input_value=tx_row[10],
            total_output_value=tx_row[11],
            addresses=tx_row[12] if tx_row[12] else [],
            inputs=inputs,
            outputs=outputs,
            metadata=metadata
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error querying transaction {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to query transaction")


@router.get("/address/{address}", response_model=List[TransactionResponse])
async def get_transactions_by_address(
    address: str,
    network: Optional[NetworkType] = Query(
        None,
        description="Network to query: 'mainnet', 'preprod', or 'preview'. Defaults to the instance's CARDANO_NETWORK setting."
    ),
    limit: int = Query(100, ge=1, le=200),
    before: Optional[datetime] = Query(
        None,
        description="Cursor pagination: return transactions strictly before this timestamp (ISO format).",
    ),
    api_key: str = Security(verify_api_key),
):
    """Get all transactions involving a specific address"""
    return await get_transactions(network=network, address=address, limit=limit, before=before, api_key=api_key)


@router.get("/stats/summary")
async def get_transaction_stats(
    network: Optional[NetworkType] = Query(
        None,
        description="Network to query: 'mainnet', 'preprod', or 'preview'. Defaults to the instance's CARDANO_NETWORK setting."
    ),
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
