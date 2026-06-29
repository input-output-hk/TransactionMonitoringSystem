# API Reference

These endpoints are served by the clustering module (FastAPI app:
[api/main.py](../backend/app/api/main.py)) and reached from the TMS single‑page app
through the `/api/clustering` reverse‑proxy: the host forwards `/api/clustering/<x>`
to the module's `/api/v1/<x>`. The paths below are written without that proxy prefix,
as the module sees them. `/api/v1` is the canonical, versioned prefix carried in the
OpenAPI schema; a bare `/api` alias serves the same routes (kept for compatibility,
omitted from the schema). Interactive docs at `/docs` (OpenAPI at `/openapi.json`).

## Authentication

- **Off by default.** Within the integrated deployment the module sits behind the
  TMS host on the compose network and is not published on its own, so it relies on
  the host's reverse‑proxy for exposure.
- When `API_KEY` is set, every endpoint **except** `/api/health` and `/api/ready`
  (and their `/api/v1` aliases) requires the header `X-API-Key: <key>` (else **401**).
- The SPA doesn't hold the key: the `/api/clustering` proxy injects `X-API-Key` into
  the forwarded request **server‑side**, so the browser never sees it. See
  [operations.md](operations.md#enabling-authentication).

## CORS

`allow_origins` comes from `CORS_ORIGINS` (comma‑separated; empty = same‑origin
only, no wildcard). Methods limited to `GET, POST, PATCH, DELETE`; headers to
`Content-Type, X-API-Key`.

## Error model

JSON `{"detail": "..."}` with standard codes: **401** (auth), **404** (not found),
**409** (a job for that target is already running), **422** (validation / bad
target / `max_txs` over cap), **429** (too many in‑flight jobs), **503**
(ClickHouse unreachable, from `/api/ready`). Internal/source errors are
**sanitized** in responses; full detail is logged server‑side.

## Health & readiness

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness. Always `{"status":"ok"}`; no DB access. Auth‑exempt. |
| GET | `/api/ready` | Readiness. Pings ClickHouse → `{"status":"ready"}` or **503**. Auth‑exempt. |

## Contracts & onboarding jobs

A scheduler auto‑onboards and auto‑classifies watched contracts as the host ingests
their transactions (the automatic feed, `FEED_ENABLED`), so the endpoints below are
the manual counterpart to that loop rather than the only way a contract gets scored.

| Method | Path | Description |
|---|---|---|
| POST | `/api/contracts` | Add a contract to the watchlist / refresh it; enqueues the onboarding pipeline. |
| POST | `/api/contracts/{target}/classify-new` | Incrementally pick up the contract's latest not‑yet‑classified transactions from the TMS's ingested chain data and score them against the contract's frozen model (no full re‑cluster). Enqueues a `classify` job. **404** if unknown, **409** if a job is already running. |
| GET | `/api/contracts` | List all watched contracts (UI dropdown source). |
| GET | `/api/contracts/{target}` | One contract's metadata + status (**404** if unknown). |
| PATCH | `/api/contracts/{target}` | Rename (set the display `label`) without re‑running the pipeline. Body: `{label}`. **404** if unknown. |
| DELETE | `/api/contracts/{target}` | Hard‑delete the contract and **all** its data across every table. **404** if unknown; **409** if a job for it is in flight (stop/wait first). Returns `{"deleted": true, "target": "..."}`. |
| GET | `/api/jobs` | List all jobs (newest first). Each carries `kind` (`onboard`\|`classify`). |
| GET | `/api/jobs/{job_id}` | One job's live status (**404** if unknown). Poll this. |

**`POST /api/contracts`** request:

```json
{ "target": "addr1...|<56-hex policy id>", "max_txs": 500, "reprocess": false }
```

- `target` (required): classified server‑side; 56‑hex → `policy`, `addr…` (bech32)
  → `address`; anything else → **422**. In the host‑backed deployment
  (`CHAIN_SOURCE=host_ch`, the docker‑compose integration) the host indexes
  transactions by address only, so a `policy` target is rejected up front with
  **422**; use an `addr…` address there.
- `max_txs` (optional): `1 .. 50000` (`MAX_TXS_CAP`); omitted = the full window the
  module is configured to fit over (`CLUSTERING_WINDOW_TXS`). A cap on an **address**
  target bounds the population to the most **recent** N of the contract's
  already‑ingested transactions; policy targets are scoped from history.
- `reprocess` (optional): re‑run the analysis over the contract's already‑ingested
  transactions (used for an in‑place refit). In the integrated module the chain data
  is always already in `tms_analytics`, so onboarding reads it in place rather than
  downloading.

Response: `{ "job_id": "job-...", "target": "...", "target_type": "address|policy" }`.
Guards: **409** if a non‑terminal job already exists for `target`; **429** if
`MAX_INFLIGHT_JOBS` non‑terminal jobs already exist.

**Contract shape** (`GET /api/contracts*`):

```json
{
  "target": "addr1...", "target_type": "address", "label": "",
  "exists": 1, "is_script": 1, "script_type": "",
  "balance_lovelace": 141000000, "asset_count": 0, "sample_tokens": "[]",
  "status": "done", "requested_max_txs": 0,
  "updated_at": "2026-06-05 09:25:57.360", "tx_count": 5000,
  "drift_score": 0.0, "reclustering_suggested": false
}
```

(`exists`/`is_script` are 0/1; `sample_tokens` is a JSON string of
`[{unit, policy_id, name}]`. `drift_score` is the trailing online-noise rate from
the incremental classifier; `reclustering_suggested` is derived at read time,
`true` once `drift_score ≥ RECLUSTER_NOISE_THRESHOLD` (default `0.25`), and the UI
surfaces it as a "re-cluster recommended" badge.)

**Job shape** (`GET /api/jobs*`):

```json
{
  "job_id": "job-abc123", "target": "addr1...", "target_type": "address",
  "max_txs": 500, "reprocess": 0,
  "status": "clustering", "stage_detail": "evaluating shape parameters",
  "txs_done": 0, "error": "",
  "created_at": "...", "updated_at": "..."
}
```

`status` ∈ `queued | checking | downloading | clustering | scoring | done | failed`.
(In the integrated module the chain data is already in `tms_analytics`, so the
`downloading` stage advances the read cursor over that data rather than fetching from
an external provider.)

## Targets (legacy / compatibility)

| Method | Path | Description |
|---|---|---|
| GET | `/api/targets` | Distinct targets present in the ingested transactions with live tx counts. Pre‑dates `contracts`; kept for compatibility. |

## Clustering

| Method | Path | Description |
|---|---|---|
| GET | `/api/evaluation?target=&feature_set=` | k‑distance curve + scored grid + recommendation. `feature_set` ∈ `shape\|graph\|combined` (default `shape`). |
| POST | `/api/cluster` | Run DBSCAN and persist a run. Body: `{target, feature_set, eps>0, min_samples>=2, notes?}`. |
| GET | `/api/runs?target=` | List cluster runs (newest first; `target` optional). |
| GET | `/api/runs/{run_id}` | One run's metadata (**404** if unknown). |
| GET | `/api/runs/{run_id}/clusters` | Per‑cluster summary stats + verdict fields (see below). |
| GET | `/api/runs/{run_id}/clusters/{cluster_id}/transactions?limit=&offset=` | Transactions in a cluster; each row carries an effective `verdict` + raw `votes`. |
| GET | `/api/runs/{run_id}/graph?limit=&cluster=` | Node/edge payload for the network view (capped, clustered‑first); each node carries `verdict`. |
| POST | `/api/runs/{run_id}/clusters/{cluster_id}/label` | Apply a manual verdict to a cluster's members. Body: `{verdict: "malicious"\|"benign", note?}`. **404** unknown run, **422** bad verdict or noise bucket (`-1`). |
| POST | `/api/runs/{run_id}/clusters/{cluster_id}/clear-label` | Remove the manual verdict from a cluster's members. |

### Verdict fields

A transaction's effective `verdict` ∈ `malicious | benign | anomaly | normal`,
resolved (highest precedence first) from: explicit per‑tx label → cluster‑inherited
label → auto‑anomaly (`votes >= 2`). Labels are stored per `tx_hash`, so they survive
reprocessing and propagate to future transactions that cluster alongside a labelled
one. Cluster‑summary rows additionally carry `verdict` (the manual label, or `null`),
`verdict_conflict` (cluster has both malicious + benign members; malicious wins),
`labeled_count`, and `anomaly_count` (members with `votes >= 2`).

## Anomaly detection

| Method | Path | Description |
|---|---|---|
| POST | `/api/anomaly` | Run the ensemble and persist a run. Body: `{target, feature_set, eps?, min_samples?, top_quantile?}`. |
| GET | `/api/anomaly-runs?target=` | List anomaly runs (newest first; `target` optional). |
| GET | `/api/anomaly-runs/{run_id}/top?limit=&offset=` | Top‑ranked anomalous transactions for a run. |

See [algorithms.md](algorithms.md) for what the clustering/anomaly parameters and
outputs mean. A fit's flagged verdicts are also published to
`tms_clustering.tx_contract_anomaly` and surfaced to the rest of the TMS as the
`contract_anomaly` attack class through `/api/analysis/results`.

## Examples

These call the module through the TMS host's `/api/clustering` proxy. From inside the
compose network you can also hit the module's `/api/v1/...` paths directly.

```bash
B=https://<tms-host>/api/clustering

# Add a watched contract (poll the returned job_id)
curl -s -X POST $B/contracts -H 'Content-Type: application/json' \
  -d '{"target":"addr1w...","max_txs":500}'
curl -s $B/jobs/job-abc123

# With auth enabled the host proxy injects X-API-Key server-side; direct calls send it
curl -s $B/contracts -H "X-API-Key: $API_KEY"

# Ad-hoc analysis of an already-ingested target
curl -s "$B/evaluation?target=addr1w...&feature_set=shape"
curl -s $B/anomaly-runs/anomaly-shape-xxxx/top?limit=20
```
