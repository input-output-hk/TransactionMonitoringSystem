# Architecture

## Overview

This is the TMS's clustering module: the contract-anomaly detection subsystem of
the Transaction Monitoring System. It runs as the `clustering` docker-compose
service (enabled with `--profile clustering`, gated by the `CLUSTERING_ENABLED`
flag) under the container name `tms-clustering-sidecar`. It is a first-party part
of the TMS, authored by the same team, not a standalone product.

The module has one runtime service and shares the TMS's databases:

- **backend** : FastAPI app (Python 3.13) exposing a JSON API, plus a Typer CLI
  sharing the same application code. Owns feature engineering, clustering,
  anomaly detection, the automatic feed scheduler and the background job worker.
- **ClickHouse** : the TMS's existing ClickHouse server. The module reads each
  watched contract's already-ingested chain data from the `tms_analytics`
  database (the same Ogmios-ingested data the core scorers use) and writes its own
  state (clusters, anomaly runs, models, classifications, the contract-anomaly
  projection) to a sibling `tms_clustering` database on that same server.
- **SPA** : the watched-contract management and cluster/anomaly drill-down views
  live in the main TMS single-page app (`frontend/`), which reaches the module
  through the host TMS API's `/api/clustering` reverse-proxy.

The service is defined in the top-level [docker-compose.yml](../../../docker-compose.yml).

## Module layering (backend)

Dependencies point strictly downward; there are no cycles.

```
                 ┌─────────────────────────────────────────┐
   entrypoints   │  api/main.py (FastAPI)     cli.py (Typer)│
                 └───────────────┬─────────────────┬────────┘
                                 ▼                 ▼
   orchestration   service/  ◀──── scheduler.py ──── jobs.py (JobManager)
                  (process_contract, cluster,   (automatic   queue + daemon worker
                   evaluate, anomaly, graph,     feed)
                   publish)
                       │        │        │        │
        ┌──────────────┘        │        │        └──────────────┐
        ▼                       ▼        ▼                       ▼
   ingest/                 clustering/  anomaly/             contracts.py
   (ingester :             (dbscan,     (detect)            (classify_target)
    drives a ChainSource)   evaluate)
        │                       │        │
        ├───────────────────────┴────────┴────────────────┐
        ▼                                                  ▼
   sources/  (ChainSource protocol,                    features/
    NormalizedTx, SourceError taxonomy)          (shape, graph, combined)
        ▼                                                  │
   sources/host_ch/  (HostChainSource :                    │
    metadata + discovery from tms_analytics)               │
        │                                                  │
        └──────────────────────┬───────────────────────────┘
                               ▼
                  storage/clickhouse/  (ClickHouseRepo : per-entity mixins;
                   HostBackedRepo reads tms_analytics, writes tms_clustering)
                               │
                               ▼
                         config.py (Settings)
```

The layering above shows the default `host_ch` source. A second `ChainSource` ships
behind the same `sources/` seam: `app/blockfrost/` (`BlockfrostSource`), selected by
`CHAIN_SOURCE=blockfrost`, which downloads an address's history over HTTP from
blockfrost.io into `tms_clustering` (the base `ClickHouseRepo`, not `HostBackedRepo`).

Responsibilities:

| Layer | Module(s) | Responsibility |
|---|---|---|
| Entrypoints | [api/main.py](../backend/app/api/main.py), [cli.py](../backend/app/cli.py) | HTTP routing / CLI commands; thin wrappers over `service`. |
| Orchestration | [service/](../backend/app/service/) (package: `pipeline` · `analysis` · `verdicts` · `online` · `scheduler` · `publish` · `_common`), [jobs.py](../backend/app/jobs.py) | The canonical pipeline, the automatic-feed scheduler that drives it, the verdict projection to `tx_contract_anomaly`, and the background worker. |
| Feed | [service/scheduler.py](../backend/app/service/scheduler.py) | The automatic feed: polls the watchlist and enqueues onboard / re-fit / classify jobs as the host ingests new transactions. No manual fetch step. |
| Publish | [service/publish.py](../backend/app/service/publish.py) | Projects resolved per-tx verdicts to `tms_clustering.tx_contract_anomaly`, the table the host reads as the `contract_anomaly` attack class. |
| Ingest | [ingest/ingester.py](../backend/app/ingest/ingester.py) | Resumable orchestration over a `ChainSource`. Under the default `host_ch` this never downloads (the host already ingested the chain, so the host-backed path reads existing data and the ingester's writes are no-ops); under `CHAIN_SOURCE=blockfrost` it drives the real download path, fetching each tx and persisting it to `tms_clustering`. |
| Algorithms | [features/](../backend/app/features/), [clustering/](../backend/app/clustering/), [anomaly/](../backend/app/anomaly/) | Feature matrices, DBSCAN, parameter evaluation, anomaly ensemble. See [algorithms.md](algorithms.md). |
| Identity | [contracts.py](../backend/app/contracts.py) | Classify a target as an address vs minting policy (pure, source‑neutral). |
| Data source | [sources/](../backend/app/sources/) (`ChainSource` protocol + factory), [sources/host_ch/](../backend/app/sources/host_ch/) (`HostChainSource`), [blockfrost/](../backend/app/blockfrost/) (`BlockfrostSource`) | The seam the analysis cores read through. `CHAIN_SOURCE=host_ch` (default) selects `HostChainSource`, which reads contract metadata and discovers transaction hashes from the host's `tms_analytics` database, with nothing fetched externally; `CHAIN_SOURCE=blockfrost` selects `BlockfrostSource`, which downloads them over HTTP from blockfrost.io. See [online-classification-design.md](online-classification-design.md). |
| Storage | [storage/clickhouse/](../backend/app/storage/clickhouse/) | All SQL. A thin repository (`ClickHouseRepo`) composed from per‑entity mixins over the HTTP client. The `HostBackedRepo` variant reads raw transaction / feature data cross-database from `tms_analytics` and writes module state to `tms_clustering`. |
| Config | [config.py](../backend/app/config.py) | Pydantic‑settings; env‑driven configuration + logging setup. |

## The canonical pipeline

`service.process_contract(...)` is the **single** path that onboards or refreshes
a contract. The automatic-feed scheduler, the job worker, and the CLI `process`
command all call it, so every contract ends in an identical state.

```
process_contract(target, target_type, max_txs, reprocess, job_id)
  1. checking    → source.metadata()                   save_contract(status=processing)
  2. read        → host-backed feature read            (no download in the
                   (skipped when reprocess)             integrated module)
  3. clustering  → load shape features ONCE
                   evaluate() → recommended (eps, min_samples)
                   cluster (DBSCAN, shape)
  4. scoring     → anomaly (shape)   [reuses the shape matrix]
                   anomaly (graph)   [loads the graph matrix once]
  5. done        → save_contract(status=done, tx_count=n)
                   publish flagged verdicts to tx_contract_anomaly
```

- In the integrated module the host TMS has already ingested the whole chain, so
  there is no download step: `process_contract` runs with `reprocess=True` and
  the host-backed path reads the watched contract's transactions directly from
  `tms_analytics`. The fit/classify population is bounded to the most **recent**
  `CLUSTERING_WINDOW_TXS` transactions of an **address** target (applied as an
  in-SQL subquery in the `HostBackedRepo`), so the fitted clusters/baselines
  reflect current traffic and DBSCAN, IsolationForest, and the O(n²) silhouette
  stay bounded for a high-volume mainnet contract. v1 watches address/script
  targets only (the host indexes by address; policy targets are rejected).
- Contracts with **< 3 transactions** skip steps 3–4 and finish `done` with a note
  (DBSCAN/evaluation need ≥ 3 points).
- Each feature matrix is built **once** and reused across evaluate/cluster/anomaly
  for that target (a deliberate performance choice; see
  [algorithms.md](algorithms.md)).
- On any failure the contract is marked `failed` (preserving previously‑saved
  metadata) and, if running under a job, the job records a **sanitized** error
  message; the full exception is logged server‑side only.

Stage names map 1:1 onto the `jobs.status` enum so the SPA can render live progress.

## The automatic feed

The module is fed automatically: an operator adds a contract to the watchlist and
the rest happens as the host ingests the chain, with no manual fetch step. The
scheduler in [service/scheduler.py](../backend/app/service/scheduler.py) is what
makes this work (host_ch only, controlled by `FEED_ENABLED`):

- Each tick (every `FEED_POLL_INTERVAL_SECONDS`) it reads the watchlist (the
  `contracts` registry) and, for every contract with no job already running,
  enqueues work through the existing single-worker `JobManager` (no new
  concurrency primitive; the single-writer invariant is preserved):
  - a `pending` contract (no model yet) gets an **onboard** fit;
  - a fitted contract whose online drift has crossed `recluster_noise_threshold`
    gets a windowed **re-fit**;
  - an otherwise-fitted contract gets an incremental **classify** of its new txs.
- Per-tick work is capped (`FEED_MAX_CONTRACTS_PER_TICK`) so a large watchlist
  cannot flood the worker, and a fitted model is refreshed at least every
  `FEED_REFIT_MAX_AGE_SECONDS` even without a drift trigger.
- On completion the pipeline publishes flagged verdicts to `tx_contract_anomaly`
  (see [service/publish.py](../backend/app/service/publish.py)) for the host to
  surface as `contract_anomaly`.

## The background job system

The API must never block on the long, sync‑ClickHouse‑heavy pipeline, so the work
the scheduler enqueues runs off the request path in [jobs.py](../backend/app/jobs.py):

- **One daemon worker thread** drains a `queue.Queue` of `job_id`s.
- For each job it runs `asyncio.run(process_contract(...))` on its own event loop
  and its own repo (the ClickHouse client is **not** thread‑safe, so each
  job/request gets an independent client).
- The worker is started/stopped by the FastAPI **lifespan** handler. On startup it
  **re‑enqueues** any non‑terminal jobs.
- **Liveness:** the worker loop guards each iteration so one bad job can't kill it,
  and `enqueue()` respawns the thread if it ever died; jobs never pile up undrained.
- **Single‑writer invariant:** only the worker writes a given job row, which is what
  makes the read‑modify‑write `update_job` safe (see [data-model.md](data-model.md)).

This requires a **single backend process** (one uvicorn worker), which is the
configured default.

## Request lifecycle (HTTP)

The module's own UI lives in the main TMS SPA (`frontend/`). The SPA never talks
to the sidecar directly; it calls the host TMS API's `/api/clustering` reverse-proxy
([backend/app/api/clustering.py](../../../backend/app/api/clustering.py)), which
forwards each request to the sidecar's `/api/v1/<path>` so the views stay
same-origin and session-authenticated.

1. The host API authenticates the request with the host's session
   (`verify_api_key`) and, gated by `CLUSTERING_ENABLED`, forwards it to the
   sidecar on the internal Docker network (`CLUSTERING_SIDECAR_URL`).
2. The sidecar's FastAPI runs its own app‑level `verify_api_key` dependency (a
   no‑op on the internal network; `/api/health` and `/api/ready` are always exempt).
3. Each sync endpoint gets a fresh per‑request repo (closed when the request
   finishes) via the `get_request_repo` dependency.
4. `POST /api/v1/contracts` validates + classifies the target, applies enqueue
   guards (dedupe + in‑flight cap), writes a `pending` contract, and the automatic
   feed picks it up from the next scheduler tick. The SPA polls
   `GET /api/v1/jobs/{id}` (through the proxy) for live progress.

See [api.md](api.md) for the full endpoint list.

## Deployment topology

The module is one service on the TMS's own compose network (`tms-network`),
enabled with `--profile clustering`. It shares the TMS ClickHouse server:
chain/feature reads come from `tms_analytics`, module state lives in
`tms_clustering`.

```
   tms-network (compose)
        host SPA ──▶ host TMS API ──/api/clustering proxy──▶ clustering:8000
                                                                 │  (tms-clustering-sidecar)
                                                                 ▼
                                                          clickhouse:8123
                                              ├── reads  tms_analytics  (host chain data)
                                              └── writes tms_clustering (module state)
        :CLUSTERING_PORT (127.0.0.1 only) ──▶ clustering:8000  (debug/observability)
```

- The sidecar is **not exposed publicly**: the host API reaches it in-network and
  its `CLUSTERING_PORT` (default 8010) binds to **loopback only**, for debugging
  and observability.
- The sidecar has a Docker **healthcheck** hitting `/api/health`.
- The backend image is **multi‑stage**: `--target runtime` builds the slim,
  **non‑root** production image (no dev deps or tests, the compose default); the
  `dev` target adds `pytest`/`ruff`/`mypy` for the in‑container test loop.
- On boot the sidecar runs `app.cli migrate`, which creates/upgrades the
  `tms_clustering` schema idempotently (see [data-model.md](data-model.md#migrations));
  it does not touch `tms_analytics`, which the core TMS owns.

## Key design decisions

- **One pipeline, no debt.** Centralizing onboarding in `process_contract` means
  every contract is processed identically and there is no ad‑hoc second path.
- **The data source is a seam.** The analysis cores depend on the
  `ChainSource` protocol ([sources/base.py](../backend/app/sources/base.py)) and a
  neutral `SourceError` taxonomy, never on a provider package. `get_source()`
  ([sources/factory.py](../backend/app/sources/factory.py)) picks the
  implementation by the `CHAIN_SOURCE` setting. The default `host_ch` selects
  `HostChainSource` ([sources/host_ch/](../backend/app/sources/host_ch/)), which
  reads contract metadata and discovers transaction hashes from the host's
  `tms_analytics`, paired with the `HostBackedRepo` for the feature reads, and
  fetches nothing externally. `CHAIN_SOURCE=blockfrost` selects `BlockfrostSource`
  ([blockfrost/](../backend/app/blockfrost/)), the downloading alternative that
  fetches an address's history from blockfrost.io. See
  [online-classification-design.md](online-classification-design.md) (Part A).
- **No data duplication under `host_ch`.** In the default mode raw transaction /
  feature reads come cross-database from the host's `tms_analytics`; the module's
  `transactions` / `tx_utxos` tables are never populated, and the host-backed ingest
  writes are no-ops. (Under `CHAIN_SOURCE=blockfrost` those tables ARE populated: the
  download path writes the fetched transactions into `tms_clustering`.) Either way,
  the module's own derived state lives in `tms_clustering`.
- **Repository pattern over ClickHouse.** All SQL lives in `ClickHouseRepo`;
  callers speak in dicts/dataclasses. Row mapping is name‑based via a single
  `_row_to_dict` helper with per‑entity column specs.
- **Pure, testable cores.** Feature builders, normalization, metadata parsing,
  DBSCAN/evaluation and the anomaly ensemble are side‑effect‑free and unit‑tested
  against fakes, with no network or ClickHouse required.
- **Off‑request background work** via a single in‑process worker thread instead of
  an external broker: the right amount of machinery for a single‑process service.
- **Findings flow one way.** The module publishes flagged verdicts to
  `tms_clustering.tx_contract_anomaly`; the host reads them as the
  `contract_anomaly` attack class through `/api/analysis/results`, gated by
  `CLUSTERING_ENABLED`. The module never writes to the host's analytics tables.
