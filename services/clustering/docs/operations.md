# Operations

How to run the clustering module securely. For what the endpoints do, see
[api.md](api.md); for how the module is wired into the host deployment (the
`clustering` compose profile, `CLUSTERING_ENABLED`), see the module
[README](../README.md).

## Enabling authentication

The module's API is unauthenticated by default so local and demo runs stay
zero-config. In the integrated deployment it is not published on its own: it
sits on the compose network behind the host's `/api/clustering` reverse proxy.
Any deployment where the module's port is reachable beyond that boundary must
enable authentication.

Three settings work together (engine names, with the compose-level variable
that feeds each one in the integrated deployment):

| Engine setting | Compose variable | Effect |
|---|---|---|
| `API_KEY` | `CLUSTERING_API_KEY` | When set, every endpoint except `/api/health` and `/api/ready` (and their `/api/v1` aliases) requires the header `X-API-Key: <key>`, else **401**. |
| `MODEL_SIGNING_KEYS` | `MODEL_SIGNING_KEYS` | Comma-separated HMAC keys for stored model blobs: sign with the first, verify against any (rotation). Unsigned blobs are pickle, that is code execution on load, so this is required wherever the ClickHouse server is not fully trusted. |
| `REQUIRE_AUTH` | `CLUSTERING_REQUIRE_AUTH` | Production safety switch: when true, startup refuses to boot unless both `API_KEY` and `MODEL_SIGNING_KEYS` are set. Set it for every network-exposed deployment so a missing key is a loud boot failure, not a silently open API. |

Steps for the integrated (compose) deployment:

1. Set `CLUSTERING_API_KEY`, `MODEL_SIGNING_KEYS`, and `CLUSTERING_REQUIRE_AUTH=1`
   in `.env` (see `.env.example`).
2. Restart the `clustering` service and the host backend.
3. Verify: a direct request without the header must return **401**, and the
   dashboard's clustering pages must still load through the proxy.

The SPA never holds the key: the host's `/api/clustering` proxy injects
`X-API-Key` into the forwarded request server-side (the host reads it as
`CLUSTERING_SIDECAR_API_KEY`, which the compose file sets from the same
`CLUSTERING_API_KEY` value), so the browser never sees it.

## CORS

`CORS_ORIGINS` (compose: `CLUSTERING_CORS_ORIGINS`) is a comma-separated origin
allowlist; empty means same-origin only, and there is no wildcard. The
integrated deployment does not need it, since the SPA reaches the module
through the host proxy on the same origin.

## Health probes

`GET /api/health` (liveness, no DB access) and `GET /api/ready` (readiness,
pings ClickHouse, **503** when unreachable) are auth-exempt by construction, so
orchestrator probes keep working with authentication enabled.

## The history backfill (HISTORY_SOURCE)

Enable by setting, in the ROOT `.env` (these are compose interpolations, like
`CLUSTERING_CHAIN_SOURCE`: a per-network `.env.<network>` file does not reach
them):

| Root .env variable | Sidecar env | Default | Meaning |
|---|---|---|---|
| `CLUSTERING_HISTORY_SOURCE` | `HISTORY_SOURCE` | (empty) | `blockfrost` or `kupo`; empty disables. host_ch only. |
| `CLUSTERING_HISTORY_MAX_TXS` | `HISTORY_MAX_TXS` | 500 | Per-contract history depth when the contract carries no cap of its own. |
| `CLUSTERING_HISTORY_MAX_TXS_CEILING` | `HISTORY_MAX_TXS_CEILING` | 5000 | Clamp on per-contract overrides (mirrors the host's backfill cap). |
| `CLUSTERING_HOST_API_URL` | `HOST_API_URL` | `http://app:8000` | kupo flavor only: the host API base. |
| `CLUSTERING_HOST_API_KEY` | `HOST_API_KEY` | (empty) | kupo flavor only: a host API key. |

The startup guards fail fast on invalid combinations (unknown value, a history
source under a blockfrost primary, a flavor missing its credential). Recreate
the `clustering` service after changing them.

Operational behavior worth knowing:

- Resume is cursor-driven. A rate-limited Blockfrost walk or a pending host-side
  kupo job is picked up again by the next classify tick (every
  `FEED_POLL_INTERVAL_SECONDS`); the completed case costs one cursor read.
  Diagnose a stalled backfill with
  `SELECT * FROM tms_clustering.ingest_cursor WHERE target = '<addr>'`:
  `done = 0` with `source = 'blockfrost'` means the walk is mid-flight or
  waiting out a provider daily limit; `source = 'kupo'` with `done = 0` means
  the host job is still running (or the trigger will be re-checked next tick).
- Quota: an onboard at the default cap costs roughly `cap + cap/100` Blockfrost
  requests (per-tx fetches plus discovery pages). The single job worker
  serializes onboards, so mass-onboarding N contracts delays live classify
  jobs by roughly N times the per-contract walk time; onboard large watchlists
  gradually.
- One-time classify churn: when a backfill completes AFTER the contract's fit
  (a rate-limit resume), the frozen model scores the history batch on the next
  tick; a pre-deployment distribution can spike the drift signal and trigger
  one re-fit, which then folds the history into the model. Expected, one-time.
- Window eviction: the fit window keeps the most recent
  `CLUSTERING_WINDOW_TXS` transactions across both sources, so once the host
  rows alone fill the window the history is no longer read; new backfills are
  then skipped up-front ("window full") instead of spending quota invisibly.
- Recall gap by design: the backfill stops a safety margin (about 36 hours)
  below the host's earliest data, so a target's last pre-deployment day may
  stay uncovered until a later refit re-walks with a higher boundary.
- Health: a deployment whose ONLY contract is history-only (no live traffic)
  never publishes verdicts, so `/health/detail` on the host reports the
  clustering block as `absent`; watch at least one live contract.
- Changing `CARDANO_NETWORK` on an existing volume requires wiping the
  module-local raw tables (`transactions`, `tx_utxos`, `tx_utxo_assets`,
  `ingest_cursor` in `tms_clustering`): they carry no network column, so the
  old network's backfilled history would poison the union reads.
