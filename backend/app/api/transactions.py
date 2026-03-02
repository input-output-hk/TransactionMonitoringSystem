"""API endpoints for querying transactions from ClickHouse"""

import json
import logging
import re
from typing import List, Optional, Dict, Any, Literal
from datetime import datetime
from fastapi import APIRouter, Query, HTTPException, Security
from pydantic import BaseModel

from app.db import clickhouse
from app.config import settings
from app.auth import verify_api_key

# Network type for API parameters
NetworkType = Literal["mainnet", "preprod"]

logger = logging.getLogger(__name__)

# Cardano tx hash: exactly 64 lowercase hex characters.
_TX_HASH_RE = re.compile(r'^[0-9a-f]{64}$')
# Cardano addresses: bech32 (addr1…/addr_test1…) and legacy base58 (Ae2…/Dzz…).
# Only alphanumeric + underscore — no SQL metacharacters possible.
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
        description="Network to query: 'mainnet' or 'preprod'. Defaults to 'preprod' if not specified."
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
        # Use provided network or default to configured network
        query_network = network or settings.CARDANO_NETWORK

        escaped_network = query_network.replace("'", "''")

        if address:
            # Use the address_transactions lookup table: one row per
            # (network, address, slot, tx_hash), ORDER BY (network, address, slot).
            # This is a B-tree point seek; has(Array, ?) on the main table would
            # degrade to a per-granule scan at scale.
            escaped_address = address.replace("'", "''")
            before_clause = f"AND t.timestamp < '{before.isoformat()}'" if before else ""
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
                    WHERE network = '{escaped_network}'
                      AND address = '{escaped_address}'
                ) at USING tx_hash
                WHERE t.network = '{escaped_network}'
                  {before_clause}
                ORDER BY t.timestamp DESC
                LIMIT {limit}
            """
        else:
            conditions = [f"network = '{escaped_network}'"]
            if before:
                conditions.append(f"timestamp < '{before.isoformat()}'")
            where_clause = "WHERE " + " AND ".join(conditions)
            query = f"""
                SELECT
                    tx_hash, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
                    input_count, output_count, total_input_value, total_output_value, addresses
                FROM transactions
                {where_clause}
                ORDER BY timestamp DESC
                LIMIT {limit}
            """

        results = await clickhouse.execute_query_async(query)

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
        description="Network to query: 'mainnet' or 'preprod'. Defaults to 'preprod' if not specified."
    ),
    api_key: str = Security(verify_api_key),
):
    """Get detailed transaction information by hash"""
    if not _TX_HASH_RE.match(tx_hash):
        raise HTTPException(status_code=422, detail="Invalid transaction hash: must be 64 lowercase hex characters")
    try:
        # Use provided network or default to configured network
        query_network = network or settings.CARDANO_NETWORK

        # Escape single quotes in tx_hash and network
        escaped_tx_hash = tx_hash.replace("'", "''")
        escaped_network = query_network.replace("'", "''")
        tx_query = f"""
            SELECT
                tx_hash, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
                input_count, output_count, total_input_value, total_output_value, addresses, metadata
            FROM transactions
            WHERE tx_hash = '{escaped_tx_hash}' AND network = '{escaped_network}'
            LIMIT 1
        """
        tx_results = await clickhouse.execute_query_async(tx_query)

        if not tx_results:
            raise HTTPException(status_code=404, detail="Transaction not found")

        tx_row = tx_results[0]

        # Get inputs
        inputs_query = f"""
            SELECT
                input_tx_hash, input_index_in_tx, address, amount, assets, is_reference, is_collateral
            FROM transaction_inputs
            WHERE tx_hash = '{escaped_tx_hash}' AND network = '{escaped_network}'
            ORDER BY input_index
        """
        inputs_results = await clickhouse.execute_query_async(inputs_query)

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

        # Get outputs
        outputs_query = f"""
            SELECT
                output_index, address, amount, assets, is_collateral
            FROM transaction_outputs
            WHERE tx_hash = '{escaped_tx_hash}' AND network = '{escaped_network}'
            ORDER BY output_index
        """
        outputs_results = await clickhouse.execute_query_async(outputs_query)

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

        # Parse metadata
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
        description="Network to query: 'mainnet' or 'preprod'. Defaults to 'preprod' if not specified."
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
        description="Network to query: 'mainnet' or 'preprod'. Defaults to 'preprod' if not specified."
    ),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    api_key: str = Security(verify_api_key),
):
    """Get transaction statistics"""
    try:
        # Use provided network or default to configured network
        query_network = network or settings.CARDANO_NETWORK

        # Escape single quotes in network
        escaped_network = query_network.replace("'", "''")
        conditions = [f"network = '{escaped_network}'"]

        if start_time:
            conditions.append(f"timestamp >= '{start_time.isoformat()}'")

        if end_time:
            conditions.append(f"timestamp <= '{end_time.isoformat()}'")

        where_clause = "WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT
                count() as total_count,
                sum(total_output_value) as total_volume,
                sum(fee) as total_fees,
                avg(total_output_value) as avg_value,
                min(timestamp) as first_tx,
                max(timestamp) as last_tx
            FROM transactions
            {where_clause}
        """

        results = await clickhouse.execute_query_async(query)
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
