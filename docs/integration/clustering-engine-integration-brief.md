# Clustering Engine Integration: Planning Brief

Status: planning input, not an approved plan.
Author: prepared on branch `feat/clustering-engine-integration` (2026-06-22).
Audience: a future Claude Code plan-mode session that will turn this into a concrete, file-level implementation plan.

## 0. How to use this brief

This document is the seed for a planning task. It captures both codebases, the
integration goal, the decisions already locked with the user, the target
architecture, and the open questions a planner must still resolve. A plan-mode
session should read this first, then read the code at the file paths in
[Section 11](#11-file-path-index-integration-surface), then produce a phased
implementation plan. Do not start editing code from this brief alone; it is a map,
not a patch.

Two repos are in play:

- Host: `/Users/ale/AP901/TransactionMonitoringSystem-ClientDelivery` (this repo). The
  real-time, whole-chain Transaction Monitoring System (TMS).
- Engine: `/Users/ale/AP901/TMS-clustering`. A per-contract clustering and anomaly
  engine. Source of the module to integrate.

## 1. Goal

Integrate the TMS-clustering engine into the host TMS as a pluggable module that can
be activated or deactivated. Transactions the engine flags as malicious surface in the
host as a new attack type, `contract_anomaly`, alongside the existing nine classes. The
integrated system must support mainnet traffic.

## 2. The two systems, and why they do not trivially compose

### 2.1 Host TMS: whole-chain, real-time, per-transaction

- Ingests the full chain live via `cardano-node` 11.0.1 + `ogmios` v6.14.0 (ChainSync,
  LocalTxMonitor, LocalStateQuery), parses to `NormalizedTransaction`, stores in
  ClickHouse (`tms_analytics`) and Postgres (lifecycle, auth, audit).
- Detection is a registry of nine stateless scorers (`backend/app/analysis/scorers/`,
  registered in `backend/app/analysis/engine.py:_build_scorers`). Each scorer is a
  `BaseScorer` with `gate(features) -> bool` then `score(features) -> ScorerResult`.
- Scoring runs in-process as an asyncio drain loop (`backend/app/tasks/analysis.py`,
  started from `backend/app/main.py` behind `ANALYSIS_ENGINE_ENABLED`). It scores each
  transaction independently and writes a nine-wide score vector to
  `tx_class_scores` (ReplacingMergeTree keyed `(network, tx_hash)`, versioned by
  `analyzed_at`).
- Output shape: `ClassScoreResult` (`backend/app/models/transaction.py`), exposed by
  `GET /api/analysis/results[/{tx_hash}]` (`backend/app/api/analysis.py`).
- Philosophy: recall first (see [CLAUDE.md](../../CLAUDE.md)). A new class must never
  silence an existing detection.

### 2.2 Engine: per-contract, population-relative, batch-fit then online-classify

- Onboard one contract (script address or minting policy id), download its whole
  transaction population, build features, cluster with DBSCAN, score anomalies with an
  ensemble, explore in a React/Cytoscape UI.
- Two canonical pipelines (do not invent a third, per the engine's CLAUDE.md):
  - Batch `service.process_contract` (`backend/app/service/pipeline.py`): full DBSCAN +
    anomaly over the contract population.
  - Incremental `service.update_contract` / `classify_new_transactions`
    (`backend/app/service/online.py`): score new transactions against a frozen
    `ShapeModel`, no re-cluster. O(clusters) per transaction.
- Anomaly ensemble (`backend/app/anomaly/detect.py`): IsolationForest (300 trees) + LOF
  (20 neighbours) + DBSCAN-noise, rank-normalised and fused to a consensus in [0,1] plus
  a vote count in 0..3. `FLAG_VOTE_THRESHOLD = 2` is load-bearing (>= 2 votes is an
  auto-anomaly). Attribution comes from `backend/app/features/explain.py` (top 3
  deviating features).
- Verdict resolution (`backend/app/service/verdicts.py`): precedence is explicit per-tx
  label > cluster-inherited label > auto-anomaly (votes >= 2) > normal.
- Feature sets (`backend/app/features/`): `shape` (per-tx numeric, euclidean), `graph`
  (Jaccard on entity co-occurrence), `combined` (shape + SVD embedding of the tx-address
  graph).
- Decoupling: the engine never imports a provider. It depends on the `ChainSource`
  protocol (`backend/app/sources/base.py`) and the `Repo` protocol
  (`backend/app/storage/protocol.py`, 37 methods). Blockfrost specifics live only in
  `backend/app/blockfrost/`. This is the key fact that makes integration clean.

### 2.3 The mismatch that drives the design

The host scores each transaction in isolation against learned per-script and per-policy
baselines. The engine's anomaly signal exists only relative to a contract's whole
transaction population and a fitted cluster model. You cannot drop the engine in as a
tenth stateless `BaseScorer`: there is no population in a single `features` dict, and the
heavy sklearn fit (DBSCAN, IsolationForest, O(n^2) Jaccard) must not run inside the
host's latency-sensitive asyncio loop.

The resolution: the engine runs as a sidecar that owns its own model lifecycle and
produces per-transaction anomaly verdicts, which are projected into the host's attack
representation as the `contract_anomaly` class. The engine stays structurally intact; the
integration is adapters plus a projection, not a rewrite.

## 3. Locked decisions (confirmed with the user)

1. Deliverable: this Markdown brief, consumed by a future plan-mode session.
2. Deployment: separate sidecar service, gated by a docker-compose profile plus a
   feature flag. Not in-process.
3. Contract scope: users add contracts to a watchlist (reuse the engine's existing
   onboarding UX, the Validators page). The host does not have a monitored-contract
   registry today; the integration brings one in from the engine.
4. Data source: feed the engine from the host's already-ingested chain data. Drop
   Blockfrost. Crucially, replace the engine's current "fetch new transactions from
   Blockfrost" button with automatic feeding: when the host's Ogmios chain sync ingests
   a transaction that touches a watched contract, that transaction becomes available to
   the engine with no manual fetch.

## 4. Target architecture

```
            Ogmios / cardano-node (host, live chain sync)
                          |
                          v
        host ClickHouse  tms_analytics
        transactions / transaction_inputs / transaction_outputs /
        address_transactions / utxo_features / tx_script_features
                          |
        (A) per-tx scorers (9 classes)        (B) watched-contract feed
        in-process drain loop                  address-match on watchlist
                |                                       |
                v                                       v
        tx_class_scores  <----- projection -----  CLUSTERING SIDECAR (new service)
        (network, tx_hash)        (D)              - ChainSource adapter over host CH
                |                                  - Repo backed by tms_clustering CH DB
                v                                  - batch fit (process_contract)
        /api/analysis/results  --- merge (E) ---   - online classify (update_contract)
                |                                  - cluster_models / anomaly_scores /
                v                                    tx_classifications / contracts / jobs
              Host UI: results + new                       |
              "Contract Anomaly" attack type   <--- cluster graph + table views (ported)
              + Watched Validators page
```

Component responsibilities:

- (A) Existing host scoring. Untouched. Recall-first guarantee preserved because the new
  class is additive (see [Section 8](#8-recall-first-and-no-magic-numbers-compliance)).
- (B) Watched-contract feed. The sidecar discovers new transactions touching watched
  contracts. Recommended mechanism: the sidecar polls the host's `tms_analytics`
  (`address_transactions` for the watched addresses, or a thin per-contract cursor) since
  its last cursor. Polling keeps the host's ingestion hot path untouched and the module
  fully decoupled and deactivatable. The planner should compare polling against a push
  hook on the ingestion path and justify the choice; default to polling.
- (C) The sidecar runs the engine's two pipelines unchanged behind the `ChainSource` and
  `Repo` adapters. Batch fit is scheduled (periodic re-cluster); online classify runs on
  the stream as new watched transactions arrive.
- (D) Projection. The sidecar writes per-transaction anomaly verdicts to a dedicated
  table (proposed `tx_contract_anomaly`, keyed `(network, tx_hash)`), carrying the
  consensus score (mapped to 0..100), the verdict, the cluster id, votes, and the
  `explain.py` attribution as evidence. See [Section 6](#6-the-new-contract_anomaly-attack-type).
- (E) Merge at read time. `GET /api/analysis/results` left-joins `tx_contract_anomaly`
  and exposes `contract_anomaly` as a synthetic entry in the `scores` vector, folding it
  into `max_score` / `risk_band` by `max(existing, contract_anomaly)`. When the module is
  off, the join is skipped and the class reads as not-applicable.

## 5. Database topology: reuse the container, separate the database

Recommendation: reuse the host's `tms-clickhouse` container; give the engine its own
ClickHouse database (proposed `tms_clustering`) on the same server. Do not stand up a
second ClickHouse container, and do not duplicate raw transaction storage.

Rationale:

- Both systems run ClickHouse 26.x. A second container would compete for the same box's
  RAM; the host already caps ClickHouse at 4GB in `docker-compose.yml`. One server,
  two databases, gives logical isolation without a second memory budget. Revisit the
  memory cap when the sidecar's batch fits run.
- The engine's own raw-tx tables (`transactions`, `tx_utxos`, `tx_utxo_assets`) overlap
  conceptually with the host's `transactions` / `transaction_inputs` /
  `transaction_outputs`. Re-ingesting from Blockfrost into engine tables would duplicate
  storage and reintroduce the dependency the user asked us to drop. Instead, implement
  the engine's `Repo` ingest/feature reads as an adapter over the host tables (cross-
  database SELECTs are fine within one server). The engine's raw-tx tables are not
  created in the integrated deployment.
- The engine's genuinely-own tables (clustering and model state) live in
  `tms_clustering`: `cluster_runs`, `cluster_labels`, `anomaly_runs`, `anomaly_scores`,
  `cluster_models`, `tx_classifications`, `contracts` (the watchlist), `jobs`.
- Postgres: the engine does not use Postgres. Keep the watchlist and jobs in
  `tms_clustering` (ClickHouse) so the sidecar stays self-contained, matching the
  engine's current design. Do not spread engine state into the host's Postgres.

A planner task: validate field-level that the host tables provide everything the
engine's `shape` and `graph` feature builders need (fees, size, in/out counts, lovelace,
unique asset count, redeemer count, timestamp for cyclical features; and stake-credential
grouping for graph entity co-occurrence). The host has `tx_script_features` (redeemers),
inputs/outputs (addresses), and `utxo_features`; confirm the mapping is complete before
committing to the adapter-over-host-tables approach. If a field is missing, the fix is a
host-side feature extraction addition, not re-ingestion.

## 6. The new `contract_anomaly` attack type

End-to-end touch points to surface engine findings as a host attack class:

1. Data model: add `CONTRACT_ANOMALY = "contract_anomaly"` to `AttackClass`
   (`backend/app/models/transaction.py:64`). Decide whether `contract_anomaly` joins
   `_CLASS_NAMES` in `backend/app/analysis/engine.py:55` or stays a read-time-only
   synthetic class. Recommendation: keep it out of `_CLASS_NAMES` so the host's per-tx
   engine never writes or clobbers it, and inject it only at API read time. This keeps
   the per-tx scoring path (and its recall guarantees) untouched.
2. Storage: new table `tx_contract_anomaly` keyed `(network, tx_hash)`, ReplacingMergeTree
   versioned by an `analyzed_at` equivalent, in `tms_clustering`. Columns: consensus score
   (0..100), raw consensus (0..1), votes, cluster id, verdict, evidence JSON (the
   `explain.py` top-feature attribution plus cluster context), model id, scored_at.
   Follow the host's additive, idempotent migration pattern (`ADD COLUMN IF NOT EXISTS`,
   register in `_EXPECTED_COLUMNS`; see `backend/app/db/clickhouse_schema.py`).
3. Score mapping: the engine emits consensus in [0,1] and a verdict. Define an explicit,
   config-driven mapping from (consensus, verdict, votes) to the host's 0..100 score and
   `RiskBand`. Auto-anomaly (votes >= 2) and cluster-inherited-malicious must land at or
   above the band the user wants alerted. No magic numbers: the mapping anchors live in
   `config/detection.yaml` under a new `contract_anomaly` section.
4. API: `GET /api/analysis/results[/{tx_hash}]` left-joins `tx_contract_anomaly`, adds
   `contract_anomaly` to the `scores` dict, and folds it into `max_score` / `max_class` /
   `risk_band` via `max(...)`. `corroboration_count` should count it like any other class.
   The join and the synthetic class are gated by the feature flag.
5. UI: minimally, the existing results views render `contract_anomaly` like the other
   classes (label, icon, band colour). Richer: port the engine's cluster graph + table
   drill-down (`ui/src` Cytoscape views) into the host UI as a "Contract Anomaly" detail
   panel, plus a "Watched Validators" management page (port the engine's Validators page:
   add, refresh, re-analyze, delete, jump to clusters/outliers), with the Blockfrost
   "fetch" action removed in favour of the automatic feed.
6. Tests: add recall tests proving a known anomalous-contract case scores into the alert
   band, and prove the existing `backend/tests/analysis/` suite stays green.

## 7. Mainnet porting and scale

The engine was built for Blockfrost-fed, contract-scoped batches. Mainnet contracts
(DEXes, lending) can have millions of transactions. Porting work:

- Graph feature set does not scale. `backend/app/features/graph.py` builds a dense
  O(n^2) Jaccard matrix capped at `MAX_GRAPH_TXS = 5000`. For high-volume contracts,
  default to `shape` features; offer `graph`/`combined` only for low-volume contracts or
  over a bounded window/sample. Make the per-contract feature-set choice explicit and
  config-driven; log when graph is skipped for volume (no silent truncation).
- Bound the batch population. Cluster over a rolling window (last N days or last N
  transactions) rather than full history, so DBSCAN batch cost stays bounded. The window
  is a config knob. Online classify against the frozen model is O(clusters) per tx and is
  the steady-state path; batch re-fit is periodic.
- Streaming fit cadence. The sidecar should: online-classify new watched transactions
  continuously (cheap), and re-fit the batch model on a schedule or on a drift trigger
  (the engine already tracks online-noise-rate drift, `recluster_noise_threshold`). Decide
  the cadence per contract and make it config-driven.
- Single-writer constraint. The engine's `update_job` is a read-modify-write safe only
  under one writer (engine CLAUDE.md invariant 3). Keep the sidecar single-process /
  single-worker for its own state; if multiple watched contracts need parallel fits, use a
  bounded job queue with sequential or carefully-isolated workers, not multiple writers to
  the same job rows.
- Throughput of the feed. The polling feed must keep up with mainnet block rate for the
  watched subset only (not the whole chain), which is bounded by watchlist size and per-
  contract volume. Size the poll interval and batch against that, mirroring the host's
  `ANALYSIS_ENGINE_*` batch/drain knobs.
- Capacity revisit. Batch fits are memory-spiky (IsolationForest 300 trees, SVD, Jaccard).
  Plan the sidecar container's memory limit and the shared ClickHouse cap together.

## 8. Recall-first and no-magic-numbers compliance

These are hard project rules (see [CLAUDE.md](../../CLAUDE.md)). The plan must honour
them explicitly:

- Recall first. `contract_anomaly` is additive: it can only raise `max_score` / band via
  `max(...)`, never lower an existing class. The host per-tx scoring path stays untouched
  (the projection is read-time). After integration, the full `backend/tests/analysis/`
  suite must still pass, and a new recall test must prove a real contract-anomaly case
  fires into the alert band. Any tuning that trades the new class's recall for precision
  needs a test showing the real-attack case still fires.
- No magic numbers. Every threshold the integration introduces (the consensus-to-score
  mapping anchors, the band cutoffs for verdict/votes, the rolling-window size, the feed
  poll interval and batch size, the per-contract feature-set and graph-volume cutoff,
  re-fit cadence and drift trigger) lives in `config/detection.yaml` (or the engine's
  validated config) and is loaded via the validated loader. Tests reference the config,
  not duplicated literals. The engine's own load-bearing constants (`FLAG_VOTE_THRESHOLD`,
  `DEFAULT_TOP_QUANTILE`, `LOF_NEIGHBORS`, `ISO_ESTIMATORS`) are documented and surfaced as
  config where the integration depends on them.

## 9. Activation and deactivation (the plugin contract)

- Compose: add a `clustering` service under a new `clustering` profile in
  `docker-compose.yml` (mirrors the existing `ingestion` / `app` profile pattern). The
  module is activated with `docker compose --profile clustering up` and is absent
  otherwise. Container name `tms-clustering-sidecar` to match the host naming convention.
- Feature flag: `CLUSTERING_ENABLED` (host settings, `backend/app/config.py`) gates the
  API read-time join and the UI surfaces. When false: the join is skipped, the
  `contract_anomaly` class reads as not-applicable, the UI hides the attack type and the
  Watched Validators page, and no sidecar work is expected.
- Migration safety: the `tx_contract_anomaly` table and the `tms_clustering` database
  creation are idempotent and gated, so an install with the module off incurs no schema
  cost beyond optional table creation. Deactivation never breaks the host.

## 10. Open questions for the planner to resolve

1. Feed mechanism: polling the host CH (recommended, decoupled) vs a push hook on the
   ingestion path (lower latency, more coupling). Confirm latency requirements for
   surfacing a contract anomaly; default to polling.
2. Field-level feature mapping: do the host's tables fully cover the engine's `shape`
   and `graph` inputs, including stake-credential entity grouping? If not, scope the
   host-side feature additions.
3. Historical backfill: a newly watchlisted contract has history that predates the
   host's sync start (or sits behind the host's ClickHouse retention TTLs). Does the
   first fit need a one-time historical backfill, and if so from where, given Blockfrost
   is being dropped? Options: accept "from sync start" populations, lengthen retention for
   watched contracts, or a narrow one-time Blockfrost backfill path kept only for
   onboarding. The user leaned away from Blockfrost; confirm the acceptable cold-start.
4. UI depth: ship only the `contract_anomaly` class in existing views first, or port the
   full cluster-graph drill-down and Validators page in the same change? Suggest phasing.
5. Model signing: the engine supports `MODEL_SIGNING_KEYS` (HMAC) for serialized model
   blobs, required in its production posture. Decide whether the integrated sidecar
   enforces it.
6. Network scoping: the host is network-aware (`network` column, preprod default). The
   engine is single-network. Confirm `contract_anomaly` and the watchlist are network-keyed
   end to end.

## 11. File-path index (integration surface)

Host (`/Users/ale/AP901/TransactionMonitoringSystem-ClientDelivery`):

- `backend/app/main.py`: lifespan, in-process tasks, `ANALYSIS_ENGINE_ENABLED` gating.
- `backend/app/tasks/analysis.py`: per-tx drain loop (the model the sidecar mirrors).
- `backend/app/analysis/engine.py`: `_CLASS_NAMES` (line 55), `_build_scorers`, per-tx
  score assembly, `tx_class_scores` write.
- `backend/app/analysis/scorers/base.py`: `BaseScorer`, `ScorerResult` (the host's
  finding shape, for reference only; the projection does not implement a scorer).
- `backend/app/analysis/scorer_config.py` + `config/detection.yaml`: validated config
  loader; add the `contract_anomaly` section here.
- `backend/app/models/transaction.py`: `AttackClass` (line 64), `RiskBand`,
  `ClassScoreResult`.
- `backend/app/db/clickhouse_schema.py`: `tx_class_scores` DDL (line ~188),
  `_EXPECTED_COLUMNS`, additive `ALTER ... ADD COLUMN IF NOT EXISTS` migration pattern.
- `backend/app/api/analysis.py`: `GET /api/analysis/results[/{tx_hash}]`; the read-time
  merge of `tx_contract_anomaly` goes here.
- `backend/app/ingestion/ogmios_client.py` + `ogmios_parser.py`: chain sync; source of
  the watched-contract feed.
- `backend/app/db/clickhouse.py`: host CH client; the feed and Repo adapter read here.
- `docker-compose.yml`: profiles (`ingestion`, `app`), `tms-clickhouse` (4GB cap),
  container naming; add the `clustering` profile and service.

Engine (`/Users/ale/AP901/TMS-clustering`):

- `backend/app/service/pipeline.py`: `process_contract` (batch fit).
- `backend/app/service/online.py`: `update_contract`, `classify_new_transactions`,
  `ensure_shape_model`, `score_shape` (incremental classify).
- `backend/app/service/verdicts.py`: verdict precedence and read decorators.
- `backend/app/anomaly/detect.py`: the ensemble; `AnomalyResult`; `FLAG_VOTE_THRESHOLD`.
- `backend/app/features/shape.py`, `graph.py`, `explain.py`: feature builders and
  attribution; `MAX_GRAPH_TXS` lives in config/`graph.py`.
- `backend/app/clustering/dbscan.py`, `evaluate.py`, `model.py`: DBSCAN, parameter
  selection, serialized `ShapeModel`.
- `backend/app/storage/protocol.py`: the `Repo` protocol (37 methods) the adapter must
  satisfy over the host's ClickHouse.
- `backend/app/sources/base.py`: `ChainSource` protocol + `SourceError` taxonomy; the
  host-data adapter implements this in place of `backend/app/blockfrost/`.
- `clickhouse/init/001_schema.sql` .. `008_contract_drift.sql`: engine tables; in the
  integrated deployment, the clustering/model tables go to `tms_clustering`; the raw-tx
  tables are replaced by the adapter-over-host-tables.
- `ui/src/`: React/Vite/Cytoscape graph + table + Validators page to port.

## 12. Suggested phasing (for the plan to expand)

1. Adapters: `ChainSource` over host CH, `Repo` over `tms_clustering` + cross-db reads.
   Prove the engine's pipelines run against host data with no Blockfrost.
2. Watchlist + automatic feed: port the contracts registry; replace the Blockfrost fetch
   with the Ogmios-driven feed (polling). No UI yet, CLI/headless first.
3. Projection + API: `tx_contract_anomaly`, the score mapping (config-driven), the
   read-time merge, the feature flag. Recall and additivity tests.
4. Sidecar packaging: container, `clustering` compose profile, scheduling of batch
   re-fit + online classify, single-writer discipline, memory sizing.
5. Mainnet scale hardening: rolling window, graph-volume cutoff, feed throughput,
   capacity revisit.
6. UI: `contract_anomaly` in existing views, then the ported cluster graph + Validators
   page.

## 13. Project constraints to carry into the plan

- Recall first; verify after every detection change (run `cd backend && pytest
  tests/analysis/`). See [CLAUDE.md](../../CLAUDE.md).
- No magic numbers; tunables go in `config/detection.yaml` via the validated loader.
- Production-grade rigor: idempotent migrations, indexes, no hardcoded fallbacks,
  chunked backfills.
- Do not git commit or push without explicit user approval.
- Docs style: no `---` rules, no em dashes, colons in headings.
