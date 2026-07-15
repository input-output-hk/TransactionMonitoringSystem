# Frontend API layer

Two sanctioned ways to call the backend. New code picks by which backend the
endpoint lives on; do not introduce a third.

## fetchWithAuth (host API: `/api/v1/*`)

The host TMS backend. Call `fetchWithAuth` (from `./fetch`) directly, then check
`res.ok` and read the JSON. On error the backend returns `{ "detail": "..." }`,
so each module extracts `detail` for its thrown `Error` message. Session cookies
ride along automatically (`credentials: "include"`); the SPA sends no API key.

Modules: `auth`, `analysis`, `stats`, `transactions`, `archive`,
`archive.client`, `notifications`, `health`.

## clustering/transport (sidecar proxy: `/api/v1/clustering/*`)

The clustering sidecar, reached through the host's reverse proxy. Go through
`clustering/transport.ts` (`get` / `send`), which wraps `fetchWithAuth` with the
`/api/v1/clustering` base path and an optional runtime response `Validator`
(the sidecar's shapes are validated at the boundary because they cross a
service seam).

## Conventions

- Payloads stay snake_case end to end. Response types mirror the backend field
  names verbatim (`full_name`, `created_at`, `block_time`); there is no
  case-mapping layer. See `auth.ts` and the note in `notifications.ts`.
- Timestamps are UTC ISO-8601 with a `Z` suffix on every endpoint; render them
  through the helpers in `lib/utils/dates`.
- List endpoints return `{ count, total, data }`; unwrap `.data` in the module.
