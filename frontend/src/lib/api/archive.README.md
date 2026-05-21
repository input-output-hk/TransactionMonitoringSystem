# Archive API — frontend wiring

Real backend implementation lives in `backend/app/api/archive.py` +
`backend/app/db/archive_queries.py`. This frontend module is a typed client
against that contract, plus a localStorage mock shim for offline dev.

## Files

- `archive.ts` — types, `ArchiveApi` interface, switch between real client
  and mock. Consumers always import `archiveApi` from here.
- `archive.client.ts` — real HTTP client. Sends `TMS-API-Key` via
  `fetchWithAuth` and the active network from `getNetwork()`.
- `archive.mock.ts` — localStorage shim under `tms-archive-mock`. Mirrors
  backend semantics: `(network, tx_hash)` identity, skip-existing bulk,
  `archived_by` from the request.
- `fetch.ts` — shared `fetchWithAuth` + `getNetwork`.

## Dev / prod switch

| Env var | Effect |
|---------|--------|
| `VITE_USE_MOCK_ARCHIVE_API=true` | Opt-in to the localStorage mock in dev (offline work). Default: real backend. Ignored in production builds. |
| `VITE_NETWORK=mainnet\|preprod\|preview` | Cardano network. Default: `preprod`. |
| `VITE_TMS_API_KEY=…` | Sent as `TMS-API-Key` header on every request. Required against a backend with `API_KEYS` configured. Leave empty against dev-mode backends. |

Production builds always use the real client regardless of the mock flag.

## Endpoint contract

All routes are guarded by `verify_api_key` (`TMS-API-Key` header). Identity
is the composite `(network, tx_hash)`.

### `POST /api/archive`

Body: `ArchiveCreateRequest` = `{ network, tx_hash, note, archived_by }`.

- 201 on insert.
- 409 if `(network, tx_hash)` already archived. The client treats 409 as
  idempotent no-op so retries don't surface as errors.

### `DELETE /api/archive/{tx_hash}?network=…`

- 204 on delete.
- 404 if not archived. Client treats 404 as no-op.

Hard delete via `ALTER TABLE … DELETE`. Restore is a sysadmin operation,
expected to be rare.

### `GET /api/archive?network=&from=&to=&limit=&offset=`

Returns `{ count, total, data: ArchiveEntry[] }`. Date range is **inclusive
on both ends**. `data[i]` includes nullable joined fields (`max_score`,
`max_class`, `risk_band`, `analyzed_at`) — null when the entry came from a
CSV import for a tx this instance never observed.

### `POST /api/archive/bulk`

Body: `{ entries: ArchiveBulkEntry[], source_label: string }`.

- Skip-existing semantics: a `(network, tx_hash)` already in `archived_alerts`
  is never overwritten. Same for duplicates within the same batch.
- Returns `{ inserted, skipped, errors }`. No `updated` counter.
- Inserted rows are tagged `source = "import:<source_label>"` so future
  exports can attribute the origin instance.

The frontend uses `source_label = "frontend-csv"` (constant).

### `GET /api/archive/export?network=&from=&to=`

Streaming CSV download — server-side generation. The frontend just builds
the URL via `archiveApi.exportUrl(...)` and either sets it on an
`<a download>` or `window.location.href` to trigger the browser download.
The output is a valid input to `POST /api/archive/bulk` on a peer instance.

CSV columns: `network, tx_hash, note, archived_by, archived_at, source`.

## Side-effects on other endpoints

The backend already excludes archived rows from `/api/analysis/results` and
`/api/analysis/stats` via an anti-join with `archived_alerts`. The frontend
therefore **does not** filter client-side anymore.

`/api/analysis/results/{tx_hash}` still returns archived alerts and
populates a `result.archived = { note, archived_by, archived_at, source }`
field for UI annotation.

## Schema (ClickHouse)

```sql
CREATE TABLE archived_alerts (
  tx_hash      String,
  network      String,
  note         String,
  archived_by  String,
  archived_at  DateTime DEFAULT now(),
  source       String DEFAULT 'local'
) ENGINE = ReplacingMergeTree(archived_at)
ORDER BY (network, tx_hash)
PARTITION BY toYYYYMM(archived_at);
```

Reads use `FINAL` for consistency.
