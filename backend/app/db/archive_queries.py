"""ClickHouse access helpers for the archive subsystem.

Same pattern as :mod:`app.db.clickhouse`: sync functions wrapped via the
shared ``_ch_executor`` ThreadPoolExecutor to keep the event loop unblocked.
All queries use parameterized SQL with dict-style placeholders.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.db.clickhouse import _ch_executor, _get_client

logger = logging.getLogger(__name__)


# Local source tag: archives created via POST /api/archive on this instance.
SOURCE_LOCAL = "local"
# Prefix applied to the source column for rows accepted via bulk CSV import.
IMPORT_SOURCE_PREFIX = "import:"


def _archive_exists(network: str, tx_hash: str) -> bool:
    rows = _get_client().execute(
        """
        SELECT 1
        FROM archived_alerts FINAL
        WHERE network = %(network)s AND tx_hash = %(tx_hash)s
        LIMIT 1
        """,
        {"network": network, "tx_hash": tx_hash},
    )
    return bool(rows)


async def archive_exists_async(network: str, tx_hash: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor,
        _archive_exists,
        network,
        tx_hash,
    )


def _archive_get(network: str, tx_hash: str) -> dict[str, Any] | None:
    rows = _get_client().execute(
        """
        SELECT note, archived_by, archived_at, source
        FROM archived_alerts FINAL
        WHERE network = %(network)s AND tx_hash = %(tx_hash)s
        LIMIT 1
        """,
        {"network": network, "tx_hash": tx_hash},
    )
    if not rows:
        return None
    note, archived_by, archived_at, source = rows[0]
    return {
        "note": note,
        "archived_by": archived_by,
        "archived_at": archived_at,
        "source": source,
    }


async def archive_get_async(network: str, tx_hash: str) -> dict[str, Any] | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor,
        _archive_get,
        network,
        tx_hash,
    )


def _archive_insert(
    network: str,
    tx_hash: str,
    note: str,
    archived_by: str,
    archived_at: datetime,
    source: str,
) -> None:
    _get_client().execute(
        """
        INSERT INTO archived_alerts
            (tx_hash, network, note, archived_by, archived_at, source)
        VALUES
        """,
        [(tx_hash, network, note, archived_by, archived_at, source)],
    )


async def archive_insert_async(
    network: str,
    tx_hash: str,
    note: str,
    archived_by: str,
) -> None:
    """Insert a single local archive entry. Caller must check existence first."""
    loop = asyncio.get_running_loop()
    archived_at = datetime.now(UTC).replace(tzinfo=None)
    await loop.run_in_executor(
        _ch_executor,
        _archive_insert,
        network,
        tx_hash,
        note,
        archived_by,
        archived_at,
        SOURCE_LOCAL,
    )


def _archive_delete(network: str, tx_hash: str) -> int:
    """Hard delete via ALTER TABLE ... DELETE. Returns rows affected (best-effort
    pre-check via FINAL count, since ClickHouse mutations are async).

    ClickHouse mutations rewrite affected parts, so they are heavyweight
    compared to row-level deletes in row-oriented stores. This is acceptable
    here because ``archived_alerts`` holds at most a few thousand admin-curated
    rows and restores are rare. A tombstone-column approach was considered and
    rejected (extra column everywhere, complicates CSV export schema).
    """
    client = _get_client()
    count_rows = client.execute(
        """
        SELECT count()
        FROM archived_alerts FINAL
        WHERE network = %(network)s AND tx_hash = %(tx_hash)s
        """,
        {"network": network, "tx_hash": tx_hash},
    )
    existing = int(count_rows[0][0]) if count_rows else 0
    if existing == 0:
        return 0
    client.execute(
        """
        ALTER TABLE archived_alerts
        DELETE WHERE network = %(network)s AND tx_hash = %(tx_hash)s
        """,
        {"network": network, "tx_hash": tx_hash},
        settings={"mutations_sync": 1},
    )
    return existing


async def archive_delete_async(network: str, tx_hash: str) -> int:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor,
        _archive_delete,
        network,
        tx_hash,
    )


_LIST_COLUMNS = (
    "network",
    "tx_hash",
    "note",
    "archived_by",
    "archived_at",
    "source",
    "max_score",
    "max_class",
    "risk_band",
    "analyzed_at",
    "_has_score",
)


def _archive_list(
    network: str,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    conditions = ["a.network = %(network)s"]
    params: dict[str, Any] = {
        "network": network,
        "limit": limit,
        "offset": offset,
    }
    if date_from is not None:
        conditions.append("a.archived_at >= %(date_from)s")
        params["date_from"] = date_from
    if date_to is not None:
        # Exclusive upper bound: the API's shared [from, to) half-open window
        # convention (app.api._params), so chained windows never double-count
        # a row archived exactly at the boundary instant.
        conditions.append("a.archived_at < %(date_to)s")
        params["date_to"] = date_to
    where = " AND ".join(conditions)
    # LEFT JOIN onto tx_class_scores so imported entries with no local
    # detection record still surface. The extra ``has_score`` projection is
    # an explicit "did this side of the join match?" marker: ClickHouse fills
    # unmatched right-side columns with type defaults (empty string, 0.0,
    # epoch) rather than NULL by default, so we cannot rely on any single
    # column value to distinguish "no match" from "match with default value".
    rows = _get_client().execute(
        f"""
        SELECT a.network, a.tx_hash, a.note, a.archived_by,
               a.archived_at, a.source,
               s.max_score, s.max_class, s.risk_band, s.analyzed_at,
               (s.tx_hash != '') AS has_score
        FROM archived_alerts AS a FINAL
        LEFT JOIN (
            SELECT tx_hash, network, max_score, max_class, risk_band, analyzed_at
            FROM tx_class_scores FINAL
            -- Restrict the right side to this network so ReplacingMergeTree
            -- prunes on ORDER BY (network, tx_hash) instead of FINAL-collapsing
            -- the entire scores table on every list page. Returned rows are
            -- unchanged: the join already requires s.network = a.network.
            WHERE network = %(network)s
        ) AS s
            ON s.tx_hash = a.tx_hash AND s.network = a.network
        WHERE {where}
        ORDER BY a.archived_at DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        d = dict(zip(_LIST_COLUMNS, row))
        if not d.pop("_has_score"):
            d["max_score"] = None
            d["max_class"] = None
            d["risk_band"] = None
            d["analyzed_at"] = None
        results.append(d)
    return results


async def archive_list_async(
    network: str,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor,
        _archive_list,
        network,
        date_from,
        date_to,
        limit,
        offset,
    )


def _archive_get_enriched(
    network: str,
    tx_hash: str,
) -> dict[str, Any] | None:
    """Single archive row with the same detection-record LEFT JOIN as the list.

    Returns ``None`` when no archive row exists for ``(network, tx_hash)``.
    The detection-record fields (``max_score``, ``max_class``, ``risk_band``,
    ``analyzed_at``) are ``None`` when the entry was imported from another
    instance for a tx this one never observed locally.
    """
    rows = _get_client().execute(
        """
        SELECT a.network, a.tx_hash, a.note, a.archived_by,
               a.archived_at, a.source,
               s.max_score, s.max_class, s.risk_band, s.analyzed_at,
               (s.tx_hash != '') AS has_score
        FROM archived_alerts AS a FINAL
        LEFT JOIN (
            SELECT tx_hash, network, max_score, max_class, risk_band, analyzed_at
            FROM tx_class_scores FINAL
            -- Pin the right side to this (network, tx_hash) so the lookup
            -- prunes on ORDER BY (network, tx_hash) instead of FINAL-collapsing
            -- the whole scores table for a single-row fetch. Both params are
            -- already bound; returned rows are unchanged.
            WHERE network = %(network)s AND tx_hash = %(tx_hash)s
        ) AS s
            ON s.tx_hash = a.tx_hash AND s.network = a.network
        WHERE a.network = %(network)s AND a.tx_hash = %(tx_hash)s
        LIMIT 1
        """,
        {"network": network, "tx_hash": tx_hash},
    )
    if not rows:
        return None
    d = dict(zip(_LIST_COLUMNS, rows[0]))
    if not d.pop("_has_score"):
        d["max_score"] = None
        d["max_class"] = None
        d["risk_band"] = None
        d["analyzed_at"] = None
    return d


async def archive_get_enriched_async(
    network: str,
    tx_hash: str,
) -> dict[str, Any] | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor,
        _archive_get_enriched,
        network,
        tx_hash,
    )


def _archive_count(
    network: str,
    date_from: datetime | None,
    date_to: datetime | None,
) -> int:
    """Total archive rows matching the network + date-range filters.

    Mirrors the WHERE clause of :func:`_archive_list` (without the JOIN with
    ``tx_class_scores`` since we only need the row count). Used to power
    paginated list responses without an extra query per page.
    """
    conditions = ["network = %(network)s"]
    params: dict[str, Any] = {"network": network}
    if date_from is not None:
        conditions.append("archived_at >= %(date_from)s")
        params["date_from"] = date_from
    if date_to is not None:
        conditions.append("archived_at <= %(date_to)s")
        params["date_to"] = date_to
    where = " AND ".join(conditions)
    rows = _get_client().execute(
        f"SELECT count() FROM archived_alerts FINAL WHERE {where}",
        params,
    )
    return int(rows[0][0]) if rows else 0


async def archive_count_async(
    network: str,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> int:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor,
        _archive_count,
        network,
        date_from,
        date_to,
    )


def _existing_pairs(pairs: list[tuple[str, str]]) -> set:
    """Return the subset of (network, tx_hash) already archived."""
    if not pairs:
        return set()
    # Build a tuple-IN clause: ClickHouse accepts tuple comparisons.
    rows = _get_client().execute(
        """
        SELECT network, tx_hash
        FROM archived_alerts FINAL
        WHERE (network, tx_hash) IN %(pairs)s
        """,
        {"pairs": pairs},
    )
    return {(r[0], r[1]) for r in rows}


def _archive_bulk_insert(
    entries: list[dict[str, Any]],
    source_label: str,
) -> dict[str, int]:
    """Skip-existing bulk insert. Returns {'inserted', 'skipped'}.

    Each entry is a dict with: network, tx_hash, note, archived_by,
    archived_at (optional), source (optional, ignored).

    Duplicates within the same batch are counted as ``skipped`` after the
    first occurrence, so the inserted count always equals the number of
    distinct ``(network, tx_hash)`` rows actually written.
    """
    if not entries:
        return {"inserted": 0, "skipped": 0}
    pairs = [(e["network"], e["tx_hash"]) for e in entries]
    existing = _existing_pairs(pairs)
    to_insert: list[tuple[Any, ...]] = []
    seen_in_batch: set = set()
    skipped = 0
    fallback_ts = datetime.now(UTC).replace(tzinfo=None)
    source_tag = f"{IMPORT_SOURCE_PREFIX}{source_label}"
    for entry in entries:
        key = (entry["network"], entry["tx_hash"])
        if key in existing or key in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(key)
        archived_at = entry.get("archived_at") or fallback_ts
        if isinstance(archived_at, datetime) and archived_at.tzinfo is not None:
            archived_at = archived_at.astimezone(UTC).replace(tzinfo=None)
        to_insert.append(
            (
                entry["tx_hash"],
                entry["network"],
                entry["note"],
                entry["archived_by"],
                archived_at,
                source_tag,
            )
        )
    if to_insert:
        _get_client().execute(
            """
            INSERT INTO archived_alerts
                (tx_hash, network, note, archived_by, archived_at, source)
            VALUES
            """,
            to_insert,
        )
    logger.debug(
        "archive bulk import from %s: inserted=%d skipped=%d",
        source_label,
        len(to_insert),
        skipped,
    )
    return {"inserted": len(to_insert), "skipped": skipped}


async def archive_bulk_insert_async(
    entries: list[dict[str, Any]],
    source_label: str,
) -> dict[str, int]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor,
        _archive_bulk_insert,
        entries,
        source_label,
    )


_EXPORT_COLUMNS = (
    "network",
    "tx_hash",
    "note",
    "archived_by",
    "archived_at",
    "source",
)


def _archive_export_rows(
    network: str,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[dict[str, Any]]:
    conditions = ["network = %(network)s"]
    params: dict[str, Any] = {"network": network}
    if date_from is not None:
        conditions.append("archived_at >= %(date_from)s")
        params["date_from"] = date_from
    if date_to is not None:
        conditions.append("archived_at <= %(date_to)s")
        params["date_to"] = date_to
    where = " AND ".join(conditions)
    rows = _get_client().execute(
        f"""
        SELECT network, tx_hash, note, archived_by, archived_at, source
        FROM archived_alerts FINAL
        WHERE {where}
        ORDER BY archived_at DESC
        """,
        params,
    )
    return [dict(zip(_EXPORT_COLUMNS, row)) for row in rows]


async def archive_export_rows_async(
    network: str,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _ch_executor,
        _archive_export_rows,
        network,
        date_from,
        date_to,
    )
