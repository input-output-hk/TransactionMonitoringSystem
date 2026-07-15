# Clustering module: documentation

Reference documentation for the TMS clustering module, the detection module that
clusters a watched contract's transactions (a script address or a minting policy
id) and flags outliers, surfacing them as the `contract_anomaly` attack class.
In its default mode (`CHAIN_SOURCE=host_ch`) it does not download chain data: it
reads each watched contract's transactions from the system's own `tms_analytics`
ClickHouse database (the same Ogmios-ingested data the core scorers use, via
`HostBackedRepo`), fits per-contract DBSCAN clusters and an anomaly ensemble, then
classifies new transactions and publishes verdicts to the sibling `tms_clustering`
database. A second source, `CHAIN_SOURCE=blockfrost`, instead downloads an arbitrary
address's transaction history over HTTP (blockfrost.io) into `tms_clustering` for
on-demand clustering of contracts the host has not ingested; the analysis is
identical either way (see [architecture.md](architecture.md)).

For how the module runs (the `clustering` compose service, the `CLUSTERING_ENABLED`
flag, the scheduler's automatic feed, build and test commands), see the module
[README](../README.md). This folder covers the detection internals: the algorithms,
data model, API surface, and design.

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | Components, module layering, the per-contract pipeline (metadata, feature read, cluster, anomaly), the background job worker, request lifecycle, key design decisions. |
| [algorithms.md](algorithms.md) | Clustering and anomaly detection in detail: feature engineering (shape / graph / combined), DBSCAN, parameter selection (k-distance knee + grid search + silhouette), and the anomaly ensemble (Isolation Forest + LOF + DBSCAN-noise). |
| [data-model.md](data-model.md) | The `tms_clustering` ClickHouse schema, table engines, `ReplacingMergeTree` + `FINAL` semantics, the cross-database feature read, timestamps, migrations, idempotency. |
| [host-boundary.md](host-boundary.md) | The host/module seam: the two ClickHouse drivers (frozen by decision), the host-to-engine column vocabulary mapping and the tx-source hooks, paired-copy modules, ClickHouse 26.x gotchas. |
| [api.md](api.md) | The module's REST API reference (reached from the SPA via the `/api/clustering` proxy): every endpoint, request/response shapes, auth, error model, examples. |
| [online-classification-design.md](online-classification-design.md) | The fit/score split that replaces batch-only DBSCAN, the re-fit/windowing strategy, and the multi-tenant execution model behind the scheduler's continuous classification. |
| [operations.md](operations.md) | Running the module securely: enabling authentication (API key, signed model blobs, the REQUIRE_AUTH boot guard), CORS, and the auth-exempt health probes. |

## At a glance

```
 tms_analytics ClickHouse (Ogmios-ingested chain data)
        │  HostBackedRepo: read a contract's txs
        ▼
 ┌──────────────┐  features (shape / graph / combined)
 │ feature read │ ───────────────────────────────────┐
 └──────────────┘                                     ▼
        ▲                          evaluate / cluster / anomaly (sklearn)
        │ scheduler (automatic feed)                  │
 ┌──────────────┐   onboard / classify per contract   │
 │  job worker  │ ◀───────────────────────────────────┘
 └──────┬───────┘
        │ publish verdicts
        ▼
 tms_clustering ClickHouse (clusters, runs, models, tx_contract_anomaly)
        │
        ▼  merged into /api/analysis/results as contract_anomaly
 ┌──────────────┐   /api/clustering proxy   ┌────────────────────────┐
 │  TMS backend │ ◀──────────────────────── │   TMS SPA (frontend/)  │
 └──────────────┘                           └────────────────────────┘
```

Both databases live on the same ClickHouse server; in the default `host_ch` mode no
chain data is duplicated (the diagram above shows that mode). The `blockfrost` source
instead stores a downloaded copy of each onboarded address's history in
`tms_clustering`. The watched-contract management and cluster/anomaly drill-down UI is
part of the main TMS SPA, not a separate interface.

## Core idea: one per-contract pipeline

Every watched contract goes through the same pipeline (metadata, feature read,
shape cluster, shape and graph anomaly, done), so every contract ends in an
identical, comparable state. The scheduler auto-onboards and auto-classifies
contracts as the chain is ingested; there is no manual fetch step. See
[architecture.md](architecture.md).
