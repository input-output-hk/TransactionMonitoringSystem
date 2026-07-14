"""Datetime helpers shared across the API layer.

ClickHouse ``DateTime`` columns are timezone-naive and the driver returns
naive Python datetimes that this codebase treats as UTC. These helpers
normalise tz-aware inputs and produce a single canonical ISO 8601 / RFC 3339
string format for API responses (``YYYY-MM-DDTHH:MM:SSZ``).
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainSerializer


def to_naive_utc(dt: datetime | None) -> datetime | None:
    """Strip tzinfo after converting to UTC. Naive input passes through.

    Used when handing a Python datetime to clickhouse_driver, which writes
    DateTime columns as naive UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def to_aware_utc(dt: datetime | None) -> datetime | None:
    """Return ``dt`` as a timezone-AWARE UTC datetime (``None`` passes through).

    Naive input is ASSUMED to already be UTC (matches what ClickHouse hands
    back); aware input is converted to UTC. The tz-aware counterpart of
    :func:`to_naive_utc`, for code that must COMPARE a naive ClickHouse timestamp
    against a tz-aware bound (e.g. an API's ``...Z`` query param) without raising
    the naive-vs-aware ``TypeError``.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_iso_utc(dt: datetime | None) -> str | None:
    """Canonical UTC encoding for API responses.

    Naive datetimes are assumed to already be UTC (matches what ClickHouse
    hands back). Returns ``None`` for ``None`` so callers can pass through
    optional timestamps without an explicit guard.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Annotated datetime for pydantic response models: serializes to the canonical
# ``...Z`` wire format on JSON output while staying a real ``datetime`` inside
# the application. Every wire-facing timestamp field uses this type so the API
# cannot emit the naive-ISO / ``+00:00`` variants that plain ``datetime``
# fields and raw ``.isoformat()`` produce.
UtcDateTime = Annotated[
    datetime, PlainSerializer(format_iso_utc, return_type=str, when_used="json")
]
