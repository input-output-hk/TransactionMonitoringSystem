"""Archive models: mark flagged transactions as known false positives.

The archive subsystem is additive: archived rows live in their own
``archived_alerts`` ClickHouse table and never mutate ``tx_class_scores``.
Cross-instance CSV import is supported, so an entry may exist for a
transaction this instance has never observed (no matching score row).
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.transaction import NetworkType

# 64-char lowercase hex. Cardano tx hashes are blake2b-256 of the tx body.
TX_HASH_PATTERN = r"^[0-9a-f]{64}$"


class ArchiveEntry(BaseModel):
    """Input shape for POST /api/archive and entries inside bulk import."""

    network: NetworkType
    tx_hash: str = Field(pattern=TX_HASH_PATTERN)
    note: str = Field(min_length=1, max_length=2000)
    archived_by: str = Field(min_length=1, max_length=200)


class ArchiveBulkEntry(ArchiveEntry):
    """Bulk import entry. Carries the original archived_at + source so a
    CSV exported from one instance round-trips cleanly into another."""

    archived_at: datetime | None = None
    source: str | None = None


class BulkArchiveRequest(BaseModel):
    # Cap kept well below the ClickHouse default max_query_size (~256 KiB).
    # The existence check sends one tuple per entry in a single IN clause;
    # 2000 entries at ~80 bytes per (network, tx_hash) tuple stays around
    # 160 KiB worst case, leaving headroom for the surrounding SQL.
    entries: list[ArchiveBulkEntry] = Field(min_length=1, max_length=2_000)
    source_label: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Identifier of the originating instance, recorded on inserted "
            "rows as 'import:<source_label>'. Local archives use 'local'."
        ),
    )


class BulkArchiveResult(BaseModel):
    inserted: int
    skipped: int
    errors: list[str] = Field(default_factory=list)


class ArchiveEntryEnriched(BaseModel):
    """GET /api/archive response row: archive metadata + the original
    detection record (when present locally) joined from tx_class_scores."""

    network: str
    tx_hash: str
    note: str
    archived_by: str
    archived_at: datetime
    source: str
    # Joined detection data; null when this archive came from a CSV import
    # for a tx this instance never observed locally.
    max_score: float | None = None
    max_class: str | None = None
    risk_band: str | None = None
    analyzed_at: datetime | None = None
