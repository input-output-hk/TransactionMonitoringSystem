"""Datetime helpers shared across the API layer.

ClickHouse ``DateTime`` columns are timezone-naive and the driver returns
naive Python datetimes that this codebase treats as UTC. These helpers
normalise tz-aware inputs and produce a single canonical ISO 8601 / RFC 3339
string format for API responses (``YYYY-MM-DDTHH:MM:SSZ``).
"""

from datetime import datetime, timezone
from typing import Optional


def to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Strip tzinfo after converting to UTC. Naive input passes through.

    Used when handing a Python datetime to clickhouse_driver, which writes
    DateTime columns as naive UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def format_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Canonical UTC encoding for API responses.

    Naive datetimes are assumed to already be UTC (matches what ClickHouse
    hands back). Returns ``None`` for ``None`` so callers can pass through
    optional timestamps without an explicit guard.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
