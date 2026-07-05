"""API endpoints for admin curation of false-positive flagged transactions.

All routes are mounted under ``/api/archive`` and guarded by ``verify_api_key``.
See ``app.models.archive`` for request/response shapes and ``app.db.archive_queries``
for the ClickHouse access layer.

ROUTE ORDER MATTERS: FastAPI matches routes in registration order. Static-
segment routes (``/bulk``, ``/export``) must be declared BEFORE the path-
parameter routes (``/{tx_hash}``), otherwise ``GET /api/archive/export`` is
captured by ``get_archived`` with ``tx_hash="export"`` and returns 404.
"""

import csv
import io
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, Security, status
from fastapi.responses import StreamingResponse

from app import audit
from app.auth import verify_api_key
from app.config import settings
from app.db import archive_queries
from app.models.archive import (
    TX_HASH_PATTERN,
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

# CSV formula-injection neutralization. note / archived_by / source are
# client-supplied free text; a value beginning with = + - @ (or a leading tab /
# CR that shifts the first cell) is interpreted as a formula by Excel / Sheets
# when the export is opened, enabling data exfiltration or command execution.
# Prefix such values with a single quote so the spreadsheet treats them as text.
# The export is designed to be re-imported to TMS, which ignores the prefix.
_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    s = "" if value is None else str(value)
    if s and s[0] in _CSV_DANGEROUS_PREFIXES:
        return "'" + s
    return s


async def _audit_suppression_intent(
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict,
    request: Request,
    actor: str,
) -> int:
    """Fail-closed intent audit shared by the three suppression endpoints.

    A suppression that cannot be audited is refused with 503: an attacker
    who could force the audit write to fail must not be able to hide a
    detection silently. ``actor`` is the authenticated principal, recorded
    server-side so the trail attributes the mutation to who actually called
    it, not to the spoofable ``archived_by`` request field.
    """
    try:
        return await audit.record_fail_closed(
            event_type="alert_suppression",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            request=request,
            actor=actor,
        )
    except audit.AuditUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit trail unavailable; alert suppression refused.",
        )


# ---------------------------------------------------------------------------
# Collection routes (no path param) — registered first so that static
# sub-paths like /bulk and /export are matched before /{tx_hash}.
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
async def archive_alert(
    entry: ArchiveEntry,
    request: Request,
    principal: str = Security(verify_api_key),
) -> dict:
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error archiving {entry.tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to archive alert")
    # Archiving SUPPRESSES an alert from active lists: for a monitoring
    # system that is the highest-impact mutation, so the audit row is
    # FAIL-CLOSED and written BEFORE the suppression (the stores live in
    # different databases, so audit-first is the only ordering that
    # guarantees no unaudited suppression). The outcome is patched in
    # best-effort afterwards.
    audit_id = await _audit_suppression_intent(
        action="archive",
        entity_type="transaction",
        entity_id=f"{entry.network}:{entry.tx_hash}",
        details={
            # archived_by is a client-supplied display label; the audit
            # actor (below, via the authenticated principal) is authoritative.
            "archived_by": entry.archived_by,
            "note": entry.note,
            "phase": "intent",
        },
        request=request,
        actor=audit.actor_from_principal(principal),
    )
    try:
        await archive_queries.archive_insert_async(
            entry.network, entry.tx_hash, entry.note, entry.archived_by,
        )
    except Exception as e:
        logger.error(f"Error archiving {entry.tx_hash}: {e}")
        await audit.append_outcome(audit_id, {"phase": "failed"})
        raise HTTPException(status_code=500, detail="Failed to archive alert")
    await audit.append_outcome(audit_id, {"phase": "applied"})
    return {
        "network": entry.network,
        "tx_hash": entry.tx_hash,
        "note": entry.note,
        "archived_by": entry.archived_by,
    }


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


@router.post("/bulk")
async def bulk_import(
    payload_request: BulkArchiveRequest,
    request: Request,
    principal: str = Security(verify_api_key),
) -> BulkArchiveResult:
    """Bulk upsert (skip-existing) used by CSV import.

    For each entry, INSERT if (network, tx_hash) is not already archived;
    otherwise skip. Local notes/attribution are never overwritten.
    """
    payload = [e.model_dump() for e in payload_request.entries]
    # Fail-closed intent row before the bulk suppression (see archive_alert).
    audit_id = await _audit_suppression_intent(
        action="bulk_archive",
        entity_type="archive_batch",
        entity_id=payload_request.source_label or "bulk",
        details={
            "entries": len(payload),
            "source_label": payload_request.source_label,
            "phase": "intent",
        },
        request=request,
        actor=audit.actor_from_principal(principal),
    )
    try:
        outcome = await archive_queries.archive_bulk_insert_async(
            payload, payload_request.source_label,
        )
    except Exception as e:
        logger.error(f"Error during bulk import: {e}")
        await audit.append_outcome(audit_id, {"phase": "failed"})
        raise HTTPException(status_code=500, detail="Failed to import archive batch")
    await audit.append_outcome(audit_id, {
        "phase": "applied",
        "inserted": outcome["inserted"],
        "skipped": outcome["skipped"],
    })
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
            "note": _csv_safe(row["note"]),
            "archived_by": _csv_safe(row["archived_by"]),
            # ISO-8601 UTC; assume naive datetimes from ClickHouse are UTC.
            "archived_at": format_iso_utc(row["archived_at"]) or "",
            "source": _csv_safe(row["source"]),
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


# ---------------------------------------------------------------------------
# Item routes (path param). Declared LAST so /bulk and /export above
# aren't shadowed by /{tx_hash}.
# ---------------------------------------------------------------------------


@router.delete(
    "/{tx_hash}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def restore_alert(
    request: Request,
    tx_hash: str = Path(pattern=TX_HASH_PATTERN),
    network: Optional[NetworkType] = Query(None),
    principal: str = Security(verify_api_key),
) -> Response:
    """Restore a transaction by hard-deleting its archive row."""
    query_network = network or settings.CARDANO_NETWORK
    # Existence check first so no-op restores keep their 404 semantics
    # without leaving intent rows; then the fail-closed intent audit (a
    # restore mutates the suppression record, so it gets the same
    # accountability as archiving). Benign race: a concurrent delete
    # between check and delete yields an intent row with phase=failed.
    #
    # Decision note (recall-first trade-off, accepted): fail-closed here
    # means an audit outage also blocks RESTORE, the one suppression
    # mutation that would surface MORE alerts. We accept that because the
    # alert was already audited when it was suppressed (its evidence trail
    # exists), restores are operator-initiated and retryable once the audit
    # store recovers, and an unaudited mutation of the suppression record
    # would break the tamper-evidence guarantee in both directions. The
    # detection/scoring pipeline is unaffected: new alerts still fire.
    try:
        exists = await archive_queries.archive_exists_async(query_network, tx_hash)
    except Exception as e:
        logger.error(f"Error restoring {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to restore alert")
    if not exists:
        raise HTTPException(
            status_code=404,
            detail=f"No archive entry found for {tx_hash} on {query_network}",
        )
    audit_id = await _audit_suppression_intent(
        action="restore",
        entity_type="transaction",
        entity_id=f"{query_network}:{tx_hash}",
        details={"phase": "intent"},
        request=request,
        actor=audit.actor_from_principal(principal),
    )
    try:
        deleted = await archive_queries.archive_delete_async(query_network, tx_hash)
    except Exception as e:
        logger.error(f"Error restoring {tx_hash}: {e}")
        await audit.append_outcome(audit_id, {"phase": "failed"})
        raise HTTPException(status_code=500, detail="Failed to restore alert")
    await audit.append_outcome(
        audit_id,
        {"phase": "applied" if deleted else "failed", "deleted": deleted},
    )
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
    tx_hash: str = Path(pattern=TX_HASH_PATTERN),
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
