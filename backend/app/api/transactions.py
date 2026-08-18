"""API endpoints for querying transactions from ClickHouse"""

import json
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Security
from pydantic import BaseModel, Field

from app.analysis.features import (
    REDEEMER_INDEX_UNKNOWN,
    assessable_datum,
    extract_redeemers,
)
from app.analysis.plutus_structure import DecodedDatum, decode_datum_structure
from app.api._params import ADDRESS_RE, NetworkParam, PageLimit, TimeFromParam, TimeToParam
from app.auth import verify_api_key
from app.config import settings
from app.db import clickhouse, raw_store
from app.models.common import ListResponse
from app.utils.datetime_utils import UtcDateTime

logger = logging.getLogger(__name__)

# Cardano tx hash: exactly 64 lowercase hex characters.
_TX_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Address shape is shared across endpoints; see app.api._params.ADDRESS_RE.

router = APIRouter(prefix="/transactions", tags=["transactions"])


class TransactionResponse(BaseModel):
    """Transaction response model"""

    tx_hash: str
    slot: int | None
    block_height: int | None
    block_hash: str | None
    block_index: int | None
    timestamp: UtcDateTime
    fee: int
    deposit: int | None
    input_count: int
    output_count: int
    total_input_value: int | None
    total_output_value: int
    addresses: list[str]


class OutputDatum(BaseModel):
    """The datum attached to one transaction output.

    A datum reaches an output two ways: inline, or as a hash whose preimage sits
    in the transaction's witness set. Both are reported, and
    ``resolved_from_witness`` says which, because "the datum is this" and "the
    datum hashes to this and here is the preimage we found" are different
    statements about a flagged transaction.
    """

    output_index: int
    datum_hash: str | None = Field(None, description="Datum hash, when the output carries one")
    resolved_from_witness: bool = Field(
        False,
        description="True when the payload came from the witness preimage map rather than inline",
    )
    size_bytes: int | None = Field(
        None,
        description="Payload size. None when the datum could not be measured, not zero.",
    )
    hex: str | None = Field(
        None,
        description="Raw CBOR hex, when the datum arrived in hex form rather than as Plutus JSON",
    )
    structure: DecodedDatum | None = Field(
        None,
        description="Decoded structure for display; see DecodedDatum.error when undecodable",
    )


class TransactionRedeemer(BaseModel):
    """One redeemer from the transaction's witness set."""

    purpose: str = Field("", description="spend / mint / publish / withdraw, '' when not stated")
    index: int = Field(
        REDEEMER_INDEX_UNKNOWN,
        description=(
            f"Ledger pointer index, or {REDEEMER_INDEX_UNKNOWN} when the payload does not state one"
        ),
    )
    hex: str | None = Field(None, description="Raw redeemer CBOR hex, when present")
    structure: DecodedDatum | None = Field(
        None,
        description="Decoded redeemer structure; redeemers are Plutus data too",
    )
    memory_units: int = Field(0, description="Execution budget: memory units")
    cpu_units: int = Field(0, description="Execution budget: CPU steps")


class TransactionDetailResponse(TransactionResponse):
    """Detailed transaction response with inputs and outputs"""

    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    metadata: dict[str, Any] | None = None
    datums: list[OutputDatum] = Field(
        default_factory=list,
        description="Per-output datums, only for outputs that carry one",
    )
    redeemers: list[TransactionRedeemer] = Field(default_factory=list)
    script_data_available: bool = Field(
        True,
        description=(
            "False when the raw Ogmios payload could not be recovered, so empty "
            "datums/redeemers mean 'unknown', not 'none'. The distinction matters "
            "on a flagged transaction and must be shown, not hidden."
        ),
    )


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


@router.get("", response_model=ListResponse[TransactionResponse])
async def get_transactions(
    network: NetworkParam = None,
    limit: PageLimit = 100,
    before: datetime | None = Query(
        None,
        description="Cursor pagination: return transactions strictly before this timestamp (ISO format).",
    ),
    address: str | None = Query(None, description="Filter by address (any input or output)"),
    api_key: str = Security(verify_api_key),
):
    """List transactions from ClickHouse."""
    if address and not ADDRESS_RE.match(address):
        raise HTTPException(status_code=422, detail="Invalid address format")
    try:
        query_network = network or settings.CARDANO_NETWORK
        params: dict[str, Any] = {"network": query_network, "limit": limit}

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
        # Cursor pagination: a filtered total would cost an extra scan with no
        # consumer (the feed pages by `before`), so total is null by contract.
        return {"count": len(transactions), "total": None, "data": transactions}

    except Exception as e:
        logger.error(f"Error querying transactions: {e}")
        raise HTTPException(status_code=500, detail="Failed to query transactions")


def _extract_script_detail(
    raw_data: dict[str, Any] | None,
) -> tuple[list[OutputDatum], list[TransactionRedeemer]]:
    """Per-output datums and the redeemer list, decoded for display.

    Reads the same witness structures the scoring path reads, through the same
    accessor (:func:`features.assessable_datum`), so the detail view and the
    detectors cannot disagree about what a transaction's datum is: notably, both
    resolve a hash-only output from the witness preimage map rather than
    reporting "no datum".

    Returns empty lists when ``raw_data`` is absent; the caller reports that as
    unavailable rather than as an absence of script data.
    """
    if not raw_data:
        return [], []
    max_nodes = settings.DATUM_DECODE_MAX_NODES
    witness_datums = raw_data.get("datums")
    witness_datums = witness_datums if isinstance(witness_datums, dict) else None

    datums: list[OutputDatum] = []
    raw_outputs = raw_data.get("outputs")
    if isinstance(raw_outputs, list):
        for index, output in enumerate(raw_outputs):
            if not isinstance(output, dict):
                continue
            payload = assessable_datum(output, witness_datums)
            datum_hash = output.get("datumHash")
            datum_hash = datum_hash if isinstance(datum_hash, str) else None
            if payload is None and datum_hash is None:
                continue
            size_bytes: int | None = None
            payload_hex: str | None = None
            if isinstance(payload, str):
                payload_hex = payload
                try:
                    size_bytes = len(bytes.fromhex(payload))
                except ValueError:
                    # Malformed hex: leave size None ("not measurable") rather
                    # than reporting a character count as a byte count.
                    size_bytes = None
            datums.append(
                OutputDatum(
                    output_index=index,
                    datum_hash=datum_hash,
                    resolved_from_witness=output.get("datum") is None and payload is not None,
                    size_bytes=size_bytes,
                    hex=payload_hex,
                    structure=decode_datum_structure(payload, max_nodes),
                )
            )

    redeemers = [
        TransactionRedeemer(
            purpose=entry.purpose,
            index=entry.index,
            hex=entry.payload_hex,
            structure=decode_datum_structure(entry.payload_hex, max_nodes),
            memory_units=entry.memory_units,
            cpu_units=entry.cpu_units,
        )
        for entry in extract_redeemers(raw_data)
    ]
    return datums, redeemers


@router.get("/{tx_hash}", response_model=TransactionDetailResponse)
async def get_transaction_by_hash(
    tx_hash: str,
    network: NetworkParam = None,
    api_key: str = Security(verify_api_key),
):
    """Get detailed transaction information by hash"""
    if not _TX_HASH_RE.match(tx_hash):
        raise HTTPException(
            status_code=422, detail="Invalid transaction hash: must be 64 lowercase hex characters"
        )
    try:
        query_network = network or settings.CARDANO_NETWORK
        params = {"tx_hash": tx_hash, "network": query_network}

        tx_results = await clickhouse.execute_query_async(
            """
            SELECT
                tx_hash, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
                input_count, output_count, total_input_value, total_output_value, addresses,
                metadata, raw_data, raw_data_truncated
            FROM transactions
            WHERE tx_hash = %(tx_hash)s AND network = %(network)s
            LIMIT 1
        """,
            params,
        )

        if not tx_results:
            raise HTTPException(status_code=404, detail="Transaction not found")

        tx_row = tx_results[0]

        inputs_results = await clickhouse.execute_query_async(
            """
            SELECT
                input_tx_hash, input_index_in_tx, address, amount, assets,
                is_reference, is_collateral, is_unspent_attempt
            FROM transaction_inputs
            WHERE tx_hash = %(tx_hash)s AND network = %(network)s
            ORDER BY input_index
            LIMIT 500
        """,
            params,
        )

        inputs = []
        for row in inputs_results:
            assets = None
            if row[4]:
                try:
                    assets = json.loads(row[4])
                except Exception:
                    assets = {"raw": row[4]}
            inputs.append(
                {
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
                }
            )

        outputs_results = await clickhouse.execute_query_async(
            """
            SELECT
                output_index, address, amount, assets, is_collateral
            FROM transaction_outputs
            WHERE tx_hash = %(tx_hash)s AND network = %(network)s
            ORDER BY output_index
            LIMIT 500
        """,
            params,
        )

        outputs = []
        for row in outputs_results:
            assets = None
            if row[3]:
                try:
                    assets = json.loads(row[3])
                except Exception:
                    assets = {"raw": row[3]}
            outputs.append(
                {
                    "index": row[0],
                    "address": row[1],
                    "amount": row[2],
                    "assets": assets,
                    "is_collateral": bool(row[4]),
                }
            )

        metadata = None
        if tx_row[13]:
            try:
                metadata = json.loads(tx_row[13])
            except Exception:
                metadata = {"raw": tx_row[13]}

        # Datum and redeemer payloads live only in the raw Ogmios blob; no column
        # holds them. An oversized blob is stored empty with raw_data_truncated=1,
        # in which case the gzipped raw store is the fallback (the same recovery
        # the scoring path uses, so both see the same bytes).
        raw_json, raw_truncated = tx_row[14], bool(tx_row[15])
        raw_data: dict[str, Any] | None = None
        if raw_json:
            try:
                raw_data = json.loads(raw_json)
            except Exception:
                logger.warning("raw_data for %s is not parseable JSON", tx_hash)
        if raw_data is None and raw_truncated and settings.RAW_STORE_ENABLED:
            timestamp = tx_row[5]
            if isinstance(timestamp, datetime):
                try:
                    # Blocking gzip read, so it goes through the store's async
                    # entry point rather than the event loop.
                    raw_data = await raw_store.read_confirmed_async(
                        query_network,
                        tx_hash,
                        timestamp,
                    )
                except Exception:
                    # Degrade to "unavailable" rather than failing the page: the
                    # inputs/outputs half of the response is still useful.
                    logger.exception("Raw store fallback failed for %s", tx_hash[:16])
        datums, redeemers = _extract_script_detail(raw_data)
        return TransactionDetailResponse(
            **_row_to_transaction(tx_row).model_dump(),
            inputs=inputs,
            outputs=outputs,
            metadata=metadata,
            datums=datums,
            redeemers=redeemers,
            script_data_available=raw_data is not None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error querying transaction {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to query transaction")


@router.get("/address/{address}", response_model=ListResponse[TransactionResponse])
async def get_transactions_by_address(
    address: str,
    network: NetworkParam = None,
    limit: PageLimit = 100,
    before: datetime | None = Query(
        None,
        description="Cursor pagination: return transactions strictly before this timestamp (ISO format).",
    ),
    api_key: str = Security(verify_api_key),
):
    """Get all transactions involving a specific address"""
    return await get_transactions(
        network=network, address=address, limit=limit, before=before, api_key=api_key
    )


class RecentBlockOut(BaseModel):
    """A block derived from grouping the transactions table (see endpoint doc)."""

    block_height: int
    block_hash: str
    timestamp: UtcDateTime | None
    tx_count: int
    total_output_value: int | None


@router.get("/blocks/recent", response_model=list[RecentBlockOut])
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
        RecentBlockOut(
            block_height=r[0],
            block_hash=r[1],
            timestamp=r[2],
            tx_count=r[3],
            total_output_value=r[4],
        )
        for r in rows
    ]


class TransactionStatsOut(BaseModel):
    total_count: int
    total_volume: int | None
    total_fees: int | None
    avg_value: float | None
    first_tx: UtcDateTime | None
    last_tx: UtcDateTime | None


@router.get("/stats/summary", response_model=TransactionStatsOut)
async def get_transaction_stats(
    network: NetworkParam = None,
    time_from: TimeFromParam = None,
    time_to: TimeToParam = None,
    api_key: str = Security(verify_api_key),
):
    """Get transaction statistics over an optional half-open [from, to) window."""
    try:
        query_network = network or settings.CARDANO_NETWORK
        params: dict[str, Any] = {"network": query_network}

        time_clauses = ""
        if time_from:
            time_clauses += " AND timestamp >= %(time_from)s"
            params["time_from"] = time_from
        if time_to:
            time_clauses += " AND timestamp < %(time_to)s"
            params["time_to"] = time_to

        results = await clickhouse.execute_query_async(
            f"""
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
        """,
            params,
        )
        row = results[0]

        return TransactionStatsOut(
            total_count=row[0],
            total_volume=row[1],
            total_fees=row[2],
            avg_value=row[3],
            first_tx=row[4],
            last_tx=row[5],
        )

    except Exception as e:
        logger.error(f"Error getting transaction stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get transaction stats")


class ThroughputOut(BaseModel):
    window_minutes: int
    count: int
    tx_per_min: float


@router.get("/stats/throughput", response_model=ThroughputOut)
async def get_transaction_throughput(
    network: NetworkParam = None,
    window_minutes: int = Query(
        5,
        ge=1,
        le=1440,
        description="Sliding window size in minutes (default 5).",
    ),
    api_key: str = Security(verify_api_key),
):
    """Recent transaction throughput.

    Counts transactions ingested in the last ``window_minutes`` and returns
    the implied ``tx/min`` rate. The dashboard's "TX / min" KPI uses this
    instead of a lifetime average so the value reflects current pipeline
    activity, not a denominator that grows forever.

    Windows on ``ingestion_timestamp``, NOT ``timestamp``: the latter is
    chain time, so during a catch-up replay every row lands hours-to-months
    in the past and a chain-time window would read 0 while the pipeline
    ingests at full speed (a false dead-pipeline alarm).

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
              AND ingestion_timestamp >= subtractMinutes(now(), %(window_minutes)s)
            """,
            {"network": query_network, "window_minutes": window_minutes},
        )
        count = int(results[0][0]) if results else 0
        return ThroughputOut(
            window_minutes=window_minutes,
            count=count,
            tx_per_min=count / window_minutes,
        )
    except Exception as e:
        logger.error(f"Error getting transaction throughput: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to get transaction throughput",
        )
