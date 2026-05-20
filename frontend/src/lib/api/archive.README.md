# Archive API — backend contract

Frontend talks to this surface via `archiveApi` from `archive.ts`. Until the
backend lands, a localStorage-backed mock (`archive.mock.ts`) implements the
same shape so the UI works offline.

To switch to the real backend in dev, set `VITE_USE_REAL_ARCHIVE_API=true`
in `.env.development.local` and restart Vite. In production builds it's
always the real client.

## Endpoints

All paths are relative to the API host already proxied at `/api`.

### `GET /api/archive`

List archive entries, paginated and filtered by `archived_at`.

Query params (all optional):

| Name | Type | Notes |
|------|------|-------|
| `from` | ISO datetime | Inclusive lower bound on `archived_at`. |
| `to` | ISO datetime | Exclusive upper bound. |
| `limit` | int, 1..1000 | Default 100. |
| `offset` | int, ≥0 | Default 0. |

Response:

```json
{
  "count": 25,
  "total": 348,
  "data": [ArchiveEntry, ...]
}
```

### `GET /api/archive/{tx_hash}`

Single entry. `200` with entry body, `404` if not archived.

### `POST /api/archive`

Archive (upsert) one alert. Body: `ArchiveCreateRequest` (JSON). Response:
the resulting `ArchiveEntry` (200/201).

Server-side:

- Stamps `archived_at` with `now()`. On upsert of an existing tx_hash, keep
  the original `archived_at` or refresh it — decide policy and document.
- Overrides `archived_by` from the authenticated user when available;
  falls back to whatever the client sent.
- Validates `reason` against the allowed list (extensible, accept "Other").

### `DELETE /api/archive/{tx_hash}`

Restore. `204` on success, `404` if the entry didn't exist (both are
acceptable to the client).

### `POST /api/archive/bulk`

Bulk upsert — used by the CSV import flow. Body:

```json
{ "entries": [ArchiveCreateRequest, ...] }
```

Response (`ArchiveBulkResponse`):

```json
{
  "inserted": 12,
  "updated": 3,
  "skipped": 1,
  "errors": [{ "row": 4, "reason": "missing tx_hash" }]
}
```

Conflict resolution: **last-write-wins** by `archived_at` (or always-wins
if the request doesn't carry a source timestamp — pick one and document).
Validation errors per row should not abort the whole batch; record them in
`errors` and continue.

## Wire types

See `archive.ts` (TypeScript source of truth):

- `ArchiveEntry` — what the backend returns
- `ArchiveCreateRequest` — what the client sends to POST endpoints
- `ArchiveListParams`, `ArchiveListResponse`
- `ArchiveBulkResponse`

## Side-effects on `/api/analysis/results`

The active alerts endpoint must exclude tx_hashes present in the archive
table (anti-join). The frontend no longer filters client-side once the
real backend lands.

## Schema sketch (ClickHouse)

```sql
CREATE TABLE tx_archive (
  tx_hash               String,
  archived_at           DateTime DEFAULT now(),
  reason                LowCardinality(String),
  notes                 String,
  archived_by           String,
  attack_type_snapshot  LowCardinality(String),
  severity_snapshot     LowCardinality(String),
  risk_score_snapshot   Float32
) ENGINE = ReplacingMergeTree(archived_at)
ORDER BY tx_hash;
```

Use `FINAL` on reads (consistent with `tx_class_scores`).
