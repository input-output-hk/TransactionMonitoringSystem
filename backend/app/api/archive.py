"""API endpoints for admin curation of false-positive flagged transactions.

All routes are mounted under ``/api/archive`` and guarded by ``verify_api_key``.
See ``app.models.archive`` for request/response shapes and ``app.db.archive_queries``
for the ClickHouse access layer.
"""

import csv
import io
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response, Security, status
from fastapi.responses import StreamingResponse

from app.auth import verify_api_key
from app.config import settings
from app.db import archive_queries
from app.models.archive import (
    ArchiveEntry,
    ArchiveEntryEnriched,
    BulkArchiveRequest,
    BulkArchiveResult,
)
from app.models.transaction import NetworkType
from app.utils.datetime_utils import format_iso_utc, to_naive_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/archive", tags=["archive"])

# Canonical CSV column order, used by both export and bulk import parsing.
CSV_COLUMNS = ("network", "tx_hash", "note", "archived_by", "archived_at", "source")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Security(verify_api_key)],
)
async def archive_alert(entry: ArchiveEntry) -> dict:
    """Mark a flagged transaction as a known false positive.

    Returns 201 on insert, 409 if (network, tx_hash) is already archived.

    Concurrency note: the existence check and the insert are not atomic.
    Two concurrent POSTs for the same (network, tx_hash) can both pass the
    409 check and both insert; ReplacingMergeTree then keeps the row with
    the latest ``archived_at``, which means the second writer silently
    overwrites the first one's note. Admin archive writes are rare and not
    concurrent in practice, so this is accepted rather than serialized.
    """
    try:
        already = await archive_queries.archive_exists_async(
            entry.network, entry.tx_hash,
        )
        if already:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Transaction is already archived for this network.",
            )
        await archive_queries.archive_insert_async(
            entry.network, entry.tx_hash, entry.note, entry.archived_by,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error archiving {entry.tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to archive alert")
    return {
        "network": entry.network,
        "tx_hash": entry.tx_hash,
        "note": entry.note,
        "archived_by": entry.archived_by,
    }


@router.delete(
    "/{tx_hash}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Security(verify_api_key)],
)
async def restore_alert(
    tx_hash: str,
    network: Optional[NetworkType] = Query(None),
) -> Response:
    """Restore a transaction by hard-deleting its archive row."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        deleted = await archive_queries.archive_delete_async(query_network, tx_hash)
    except Exception as e:
        logger.error(f"Error restoring {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to restore alert")
    if deleted == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No archive entry found for {tx_hash} on {query_network}",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{tx_hash}",
    response_model=ArchiveEntryEnriched,
    dependencies=[Security(verify_api_key)],
)
async def get_archived(
    tx_hash: str,
    network: Optional[NetworkType] = Query(None),
) -> ArchiveEntryEnriched:
    """Single archive entry enriched with the original detection record.

    Returns 404 when ``(network, tx_hash)`` is not archived. Lets the UI
    fetch one row without paginating the whole list — useful for deep links
    into ``/archive/{tx_hash}``.
    """
    query_network = network or settings.CARDANO_NETWORK
    try:
        row = await archive_queries.archive_get_enriched_async(
            query_network, tx_hash,
        )
    except Exception as e:
        logger.error(f"Error fetching archive entry {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch archive entry")
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No archive entry for {tx_hash} on {query_network}",
        )
    return ArchiveEntryEnriched(**row)


@router.get("", dependencies=[Security(verify_api_key)])
async def list_archived(
    network: Optional[NetworkType] = Query(None),
    date_from: Optional[datetime] = Query(
        None, alias="from",
        description="ISO timestamp; lower bound on archived_at (inclusive)",
    ),
    date_to: Optional[datetime] = Query(
        None, alias="to",
        description="ISO timestamp; upper bound on archived_at (inclusive)",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """List archived alerts enriched with the original detection record."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        from_naive = to_naive_utc(date_from)
        to_naive = to_naive_utc(date_to)
        rows = await archive_queries.archive_list_async(
            network=query_network,
            date_from=from_naive,
            date_to=to_naive,
            limit=limit,
            offset=offset,
        )
        total = await archive_queries.archive_count_async(
            network=query_network,
            date_from=from_naive,
            date_to=to_naive,
        )
    except Exception as e:
        logger.error(f"Error listing archive: {e}")
        raise HTTPException(status_code=500, detail="Failed to list archive")
    data = [ArchiveEntryEnriched(**r) for r in rows]
    return {"count": len(data), "total": total, "data": data}


@router.post("/bulk", dependencies=[Security(verify_api_key)])
async def bulk_import(request: BulkArchiveRequest) -> BulkArchiveResult:
    """Bulk upsert (skip-existing) used by CSV import.

    For each entry, INSERT if (network, tx_hash) is not already archived;
    otherwise skip. Local notes/attribution are never overwritten.
    """
    payload = [e.model_dump() for e in request.entries]
    try:
        outcome = await archive_queries.archive_bulk_insert_async(
            payload, request.source_label,
        )
    except Exception as e:
        logger.error(f"Error during bulk import: {e}")
        raise HTTPException(status_code=500, detail="Failed to import archive batch")
    return BulkArchiveResult(
        inserted=outcome["inserted"],
        skipped=outcome["skipped"],
        errors=[],
    )


@router.get("/export", dependencies=[Security(verify_api_key)])
async def export_csv(
    network: Optional[NetworkType] = Query(None),
    date_from: Optional[datetime] = Query(None, alias="from"),
    date_to: Optional[datetime] = Query(None, alias="to"),
) -> StreamingResponse:
    """Download archive entries as RFC 4180 CSV. The output file is a valid
    input to POST /api/archive/bulk on another TMS instance."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        rows = await archive_queries.archive_export_rows_async(
            network=query_network,
            date_from=to_naive_utc(date_from),
            date_to=to_naive_utc(date_to),
        )
    except Exception as e:
        logger.error(f"Error exporting archive: {e}")
        raise HTTPException(status_code=500, detail="Failed to export archive")

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    for row in rows:
        writer.writerow({
            "network": row["network"],
            "tx_hash": row["tx_hash"],
            "note": row["note"],
            "archived_by": row["archived_by"],
            # ISO-8601 UTC; assume naive datetimes from ClickHouse are UTC.
            "archived_at": format_iso_utc(row["archived_at"]) or "",
            "source": row["source"],
        })
    buf.seek(0)

    # File name encodes the query so two exports from the same instance don't
    # clobber each other in a typical download folder.
    from_tag = date_from.date().isoformat() if date_from else "all"
    to_tag = date_to.date().isoformat() if date_to else "all"
    filename = f"tms-archive-{query_network}-{from_tag}-{to_tag}.csv"

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


