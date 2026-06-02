"""Shared helpers for the tx_class_scores re-score / re-classify one-offs.

Used by ``reclassify_for_tuning_2026_06_01.py`` (full re-analysis) and
``migrate_risk_band_low_to_informational.py``. Centralises the settings-based
ClickHouse connection, tolerant JSON parsing, the re-score summary print, and
the ReplacingMergeTree re-insert + per-partition OPTIMIZE, so callers cannot
drift on the correctness-critical write path.
"""

import json
from collections import Counter

from clickhouse_driver import Client

from app.config import settings
from app.db import clickhouse


def connect() -> Client:
    """ClickHouse client from settings, so the same script runs unchanged
    against local preprod and the server (do not hardcode localhost)."""
    return Client(
        host=settings.CLICKHOUSE_HOST,
        port=settings.CLICKHOUSE_PORT,
        user=settings.CLICKHOUSE_USER,
        password=settings.CLICKHOUSE_PASSWORD,
        database=settings.CLICKHOUSE_DB,
    )


def loads(value, default):
    """Tolerant JSON parse for a stored string column; ``default`` on empty/bad."""
    if isinstance(value, str) and value and value != "{}":
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def report(corrected, prev_classes):
    """Print the re-score summary (count, max_class churn, new band/class mix)."""
    changed = sum(1 for r, pc in zip(corrected, prev_classes) if r["max_class"] != pc)
    print(f"re-scored {len(corrected)} rows; max_class changed on {changed}")
    print("new risk_band:", dict(Counter(r["risk_band"] for r in corrected)))
    print("new max_class:", dict(Counter(r["max_class"] or "(none)" for r in corrected)))


def write(client, corrected):
    """Insert corrected rows and OPTIMIZE each touched partition so the dedupe
    takes effect immediately rather than waiting for background merges."""
    clickhouse.insert_class_scores(corrected)
    partitions = sorted({r["analyzed_at"].strftime("%Y%m%d") for r in corrected})
    for part in partitions:
        client.execute(f"OPTIMIZE TABLE tx_class_scores PARTITION {part} FINAL")
    print(f"\nInserted {len(corrected)} corrected rows; merged {len(partitions)} partitions.")
