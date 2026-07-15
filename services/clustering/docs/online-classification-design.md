# Design: online classification at mainnet scale

Status: **implemented** (shape feature set; remaining items tracked in
[Phasing](#phasing)). Goal: classify *incoming* transactions into clusters and
anomaly verdicts, continuously, across ~100 watched contracts, driven by the
TMS's own already-ingested chain data. This document records the data-source
seam, the fit/score split that replaces batch-only DBSCAN, the re-fit/windowing
strategy, and the multi-tenant execution model. It reshapes the *analysis and
ingestion cores*; the storage model and contract/registry layers largely
survive.

The module reads each watched contract's transactions from the TMS's
`tms_analytics` ClickHouse database (the Ogmios-ingested chain data the core
scorers already use) via `HostBackedRepo`, selected by `CHAIN_SOURCE=host_ch`.
Its own state (clusters, anomaly runs, models, classifications, the
`tx_contract_anomaly` projection) lives in the sibling `tms_clustering` database
on the same server. Under `host_ch` there is no external download: the data is
already in the system when a contract is onboarded. (The alternative
`CHAIN_SOURCE=blockfrost` instead downloads the address's history over HTTP; see the
source-selection note below.)

## Why this is needed (one paragraph)

Batch fit re-clusters a contract's **entire (windowed) history** in one pass
([service/](../backend/app/service/) → feature read → DBSCAN `fit_predict`), the
graph path is a dense O(n²) Jaccard matrix hard-capped at `MAX_GRAPH_TXS=5000`,
all rows are materialized into pandas, and a single worker thread serializes
jobs ([jobs.py](../backend/app/jobs.py)). Reading the host's chain data removes
the per-tx fetch wall but not these. DBSCAN has no `predict()` for new points,
so "classify incoming txs" cannot be expressed by the batch fit alone: it can
only re-run everything. The fit/score split below is what makes continuous
classification cheap.

## Invariants to preserve

These are load-bearing and the design keeps them:

1. **One pipeline.** `process_contract` stays the canonical *fit* path; *score*
   is a narrower path that consumes a fitted model, not a second ingest.
2. **`FINAL` on fact-table reads.** Unchanged; the host's
   `transactions`/`transaction_outputs` stay `ReplacingMergeTree`, and every
   cross-database read applies `FINAL`.
3. **Single writer per job/row.** We keep this property while adding workers, by
   *partitioning by contract* (below) rather than locking.
4. **Per-tx labels are the durable unit.** `cluster_labels` keyed by `tx_hash`
   already survives reprocessing; the score path and cluster-alignment build on
   that, not on cluster IDs.

## Part A: data source as a removable module

> **Status: implemented.** The `ChainSource` seam ships: `app/sources/base.py`
> (protocol + `NormalizedTx` + the `SourceError`/`SourceNotFound`/`SourceRateLimited`
> taxonomy) and `app/sources/factory.py` (`get_source` keyed by `CHAIN_SOURCE`).
> The integrated deployment defaults to `CHAIN_SOURCE=host_ch`:
> `app/sources/host_ch/source.py` (`HostChainSource`) reads the host TMS's
> already-ingested chain data from `tms_analytics`, paired with `HostBackedRepo`
> for the feature reads, and uses no external download path. A second implementation,
> `app/blockfrost/source.py` (`BlockfrostSource`), ships behind the same seam and
> DOES download over HTTP (from blockfrost.io) when `CHAIN_SOURCE=blockfrost`. The
> seam is the single point of source selection; the ingester/service/CLI are typed to
> `ChainSource` and import no provider package (blockfrost is imported lazily only in
> the factory).
> One deviation from the protocol sketch below, made so the seam stays a pure,
> behaviour-preserving abstraction: discovery is `tx_hash_pages(...)` with
> explicit resume params (host_ch resumes by slot: `slot:<n>`) rather than a
> single opaque `discover(cursor=…)`.

**Goal:** the data source is one implementation behind an interface, so the
host-backed source (`HostChainSource`) is the integrated default and no analysis
code changes when the source changes. Under `host_ch` the "source" never
downloads anything: it resolves a watched contract's transactions from the
host's chain data and the feature reads come straight from the host tables.

### The seam

Everything downstream reads only the host's chain tables (`transactions`,
`transaction_inputs`, `transaction_outputs`); features/clustering/anomaly never
touch the source. So the *contract* a source must satisfy is "resolve a watched
target's transactions and the metadata to identify it." The protocol in
`app/sources/base.py`:

```python
class ChainSource(Protocol):
    """Everything the engine needs from a chain data source. Implementations
    own their own wire-format → domain-record normalization (host_ch normalizes
    nothing: the records are already in the host's tables)."""

    async def tx_hash_pages(
        self, *, address, policy_id, cursor, mode, ...
    ) -> AsyncIterator[tuple[str, list[str]]]:
        """Resumable pages of (cursor, tx_hashes) touching the target."""

    async def fetch_tx(self, target, target_type, tx_hash) -> NormalizedTx:
        """One tx → (TxRecord, [UtxoRecord], [AssetRecord]). host_ch raises:
        the integrated sidecar never fetches an individual transaction."""

    async def metadata(self, target, target_type) -> TargetMeta:
        """Address/script metadata for contract identification."""
```

Note the key move: **the read path is source-owned.** Under `host_ch`,
`HostChainSource.tx_hash_pages` pages a watched address's hashes from the host's
`address_transactions` by slot, and the actual feature columns are read by
`HostBackedRepo` straight from the host's `transactions` /
`transaction_outputs` / `tx_script_features` (FINAL, cross-database, on the same
server). `fetch_tx` raises by design: in the integrated deployment a per-tx
fetch is a wiring bug, not a normal path, because the data is already present.

### What lives where

| Concern | host_ch (integrated) | Notes |
|---|---|---|
| Chain ingestion | the host TMS (Ogmios → `tms_analytics`) | the module never ingests raw chain data |
| Target → tx-hash discovery | `HostChainSource.tx_hash_pages` (indexed query on `address_transactions`, by slot) | no per-tx fan-out, no rate ceiling |
| Feature reads | `HostBackedRepo` (cross-database `tms_analytics` → engine-shaped columns) | three host-side gaps bridged in SQL (size, distinct_assets, redeemer_count) |
| Engine-owned state | `tms_clustering` (clusters, runs, models, classifications, `tx_contract_anomaly`) | sibling database, same server |
| Source selection | `app/sources/factory.py` keyed by `CHAIN_SOURCE` | `host_ch` for the integrated module |

Because the source surfaces only `NormalizedTx` / target metadata and the
feature columns flow through `HostBackedRepo`, no code in
features/clustering/anomaly/storage knows where the data came from.

### Discovery and the rolling window

`HostChainSource.tx_hash_pages` resumes by slot (`cursor = slot:<n>`); a `tip`
walk re-covers from the cursor's slot and is idempotent, because the host rows
are append-only and the engine classifies each hash once. There is no rollback
seam to manage in the module: the host's own ingester owns chain-fork handling,
and the host tables are `ReplacingMergeTree`, so a re-applied block is a no-op
on the read side.

The fit/classify population is bounded by a **rolling window**
(`CLUSTERING_WINDOW_TXS`, default 50,000): `HostBackedRepo` resolves only the
most recent N distinct tx-hashes that touched the watched address, applied as an
in-SQL subquery (never a multi-thousand-element array parameter) so the window,
the fit, and every read agree and stay bounded. The window is also the sidecar's
hard memory bound: it keeps DBSCAN, IsolationForest, and the O(n²) silhouette
bounded for a high-volume mainnet contract. `0` means unbounded, for
small/test contracts only.

### Why there is no streaming `TipSource`

Continuous classification does not need a chainsync seam in the module, because
the host already follows the tip for the whole chain. The "incremental" behavior
is the scheduler's: as the host ingests new transactions into `tms_analytics`,
the feed (Part D) re-runs the cheap classify path over the contract's
not-yet-classified hashes against its frozen model. `metadata()` stays on the
source for onboarding (existence + script-ness, read from the host's address
header); balance/token enrichment is left to the host's own views (display-only,
not needed to fit).

## Part B: fit/score split (the classifier)

DBSCAN can't predict new points, so we stop asking it to. We split into:

### Fit (batch, per contract, periodic): extends the canonical pipeline

Runs DBSCAN + the anomaly ensemble on a bounded **reference set** (Part C), then
**persists a model artifact** instead of only writing a run:

```
ClusterModel(
  model_id, target, feature_set, schema_version,
  scaler_params,                # RobustScaler median/IQR, to transform new txs
  eps, min_samples,             # pinned for the score path
  clusters=[                    # one per DBSCAN label (excl. noise)
    {cluster_id, centroid|medoid, radius, member_tx_count,
     graph_signature}          # MinHash of the cluster's address set (graph fs)
  ],
  iso_model, lof_model,         # fitted, novelty=True, support score_samples()
  fitted_at, reference_window
)
```

- IsolationForest already scores new points (`score_samples`). LOF must be fit
  with `novelty=True` to score unseen points (batch mode is outlier-mode
  `fit_predict`; this changes for the persisted model only).
- For the **graph** feature set there is no euclidean centroid (distances are
  precomputed Jaccard). Store a **MinHash signature per cluster** (the union of
  member address sets); a new tx is assigned by approximate Jaccard distance to
  each cluster signature: O(clusters), not O(n²).

### Score (online, per incoming tx): the narrow path

For each new tx the host has ingested:
1. Build its feature row (shape: per-tx, already cheap; graph: its address set).
2. Transform with the persisted `scaler_params`.
3. **Assign**: nearest cluster centroid within `radius`/`eps` → that cluster;
   else `unassigned` (the online analogue of DBSCAN noise). Graph: nearest
   cluster MinHash signature.
4. **Score**: `iso_model.score_samples` + `lof_model.score_samples`; combine
   into the same consensus vote the batch ensemble uses today.
5. Apply **inherited verdict**: if the assigned cluster carries a manual
   `cluster_labels` verdict, the tx inherits it; this is exactly the batch
   "propagate to co-clustered txs" rule, now applied at classify time.
6. Write one `tx_classifications` row (tx_hash, model_id, cluster_id, scores,
   verdict). Cost is O(clusters) per tx, independent of history size.

This is what makes the system scale: **per-update cost ∝ new txs, not ∝ total
history.** DBSCAN runs only at fit time, on a bounded set.

### New tables

| Table | Engine | Purpose |
|---|---|---|
| `cluster_models` | `ReplacingMergeTree(fitted_at)` | versioned fit artifacts per (target, feature_set) |
| `tx_classifications` | `ReplacingMergeTree(scored_at)` | online verdict per (tx_hash, model_id) |

Both tables live in the engine's `tms_clustering` database. The feature store is
the host's `tms_analytics` chain tables, read through `HostBackedRepo`; the
shape aggregation runs in ClickHouse SQL (cross-database, FINAL) rather than
pulling raw rows into Python.

## Part C: re-fit cadence and windowing

> Would it make sense to re-run clustering only every X days, and only on the Y
> latest transactions?

**Yes: that is the right shape for the *fit* step.** It bounds compute (caps the
O(Y²) graph and the in-memory load) and decouples expensive batch work from the
cheap continuous scoring in Part B. But naïve "Y latest by recency" creates
three real problems. Address all three:

**1. Baseline drift / masking (the dangerous one).** Anomaly scores are
*relative to the fitted baseline*. If the baseline is only recent data, a slow
or sustained attack becomes "the new normal" and stops being flagged: the
window normalizes the very behavior you want to catch. Mitigation: the fit set
is not pure recency (below), and IsolationForest/LOF baselines change only at
re-fit, so a drift detector (next) decides *when* that's allowed to happen.

**2. Eviction of rare/periodic patterns and labeled examples.** A recency window
can drop the historical txs that *defined* a cluster (low-volume periodic
behavior) and can drop manually-labeled txs, weakening label propagation.

**3. Cluster-identity churn.** DBSCAN cluster IDs are arbitrary; re-fitting
renumbers/splits/merges them. The verdict-labeling UI in the TMS SPA, and any
analyst watching a contract over time, need *stable* identity.

### The fix: representative reference set + alignment + triggered re-fit

- **Reference set = `all labeled txs` ∪ `stratified sample across full history`
  ∪ `recent window`**, capped at ≤ `MAX_GRAPH_TXS` for the graph feature set.
  This keeps the baseline representative (fixes #1, #2) while staying bounded.
  The recent window keeps the model current; the historical sample + all labels
  keep it from forgetting.
- **Cross-run cluster alignment.** After each fit, match new clusters to the
  previous model's clusters by tx-set Jaccard / centroid proximity; carry the
  stable `cluster_id` and any verdict forward (fixes #3). Per-tx labels already
  survive via `cluster_labels`; alignment makes the *cluster* continuity visible.
- **Re-fit cadence = max(every X days, drift trigger).** Re-fit on a schedule
  *and* early when the **unassigned/noise rate among recently scored txs** crosses
  a threshold; that rate rising means incoming txs no longer match the model, i.e.
  it's stale. This is strictly better than a fixed X: cheap contracts re-fit
  rarely, drifting ones re-fit promptly. The drift trigger is implemented (Part C
  noise-rate signal, `RECLUSTER_NOISE_THRESHOLD`) and acted on by the feed; the
  representative reference set and cross-run alignment remain tracked work.

So the refined answer: **re-fit every X days (or on drift) on a representative,
bounded reference set, not pure "Y latest", and classify everything in between
online against the frozen model, with cluster alignment to keep IDs/labels
stable.** Plain "every X days on the Y latest" works only for low-stakes,
stationary contracts; for anomaly detection specifically, the recency-only
baseline is a correctness bug, not just an efficiency choice. In the integrated
module the "Y latest" bound is the rolling window `CLUSTERING_WINDOW_TXS`
(Part A); the representative-reference-set refinement and cross-run alignment
are tracked in [Phasing](#phasing).

## Part D: multi-tenant execution (100 contracts)

The integrated module runs a single job worker today; the feed scheduler bounds
per-tick work so a large watchlist cannot flood it (`FEED_MAX_CONTRACTS_PER_TICK`).
The path to true parallelism preserves the single-writer invariant:

- **Worker pool of N, partitioned by contract**: `worker = hash(target) % N`. A
  given contract is always handled by the same worker, so `update_job`'s
  read-modify-write stays single-writer *per job*; invariant #3 is preserved by
  construction, not by locking. Contracts run in parallel; a contract never does.
- **Two job classes**: heavy `fit`/`onboard` jobs (scheduled/drift-triggered)
  and light `classify` work (high-throughput). Keep them on separate queues so a
  long fit never starves scoring.
- Job/run state stays in ClickHouse (`tms_clustering`) today; it could move to a
  control-plane database with `SELECT ... FOR UPDATE` if a future deployment
  prefers one. The worker pool is tracked in [Phasing](#phasing).

The continuous behavior is already in place via the scheduler (Part A, "Why
there is no streaming `TipSource`"): a per-tick poll of the watchlist enqueues an
onboard fit, a windowed re-fit (on drift), or an incremental classify per
non-busy contract, so a watched contract is scored automatically as the host
ingests its transactions, with no manual fetch step.

## Part E: remaining compute walls and mitigations

| Wall | Mitigation in this design |
|---|---|
| Dense O(n²) Jaccard graph, 5k cap | Bound by the rolling window for *fit*; planned: **MinHash/LSH** for both the fit signatures and online assignment, approximate Jaccard without the dense matrix |
| Whole-dataset-into-pandas | Fit reads only the windowed population; shape aggregation runs in ClickHouse SQL over the host tables |
| DBSCAN no predict | Solved: score path uses centroid + persisted IsolationForest/LOF, never DBSCAN |
| Per-refresh cost ∝ history | Solved: scoring is O(clusters)/tx; DBSCAN only at bounded (windowed) fit |

## Phasing

0. **Source interface.** Extract `ChainSource`, with the source-owned read path.
   *(de-risks all below)* **Shipped.** `app/sources/base.py` + factory; the
   integrated `HostChainSource` (`host_ch`) reads the host TMS's chain data.
1. **Fit artifact + score path.** Persist `ClusterModel`; add online scoring and
   `tx_classifications`. Validate that online scores match a full batch re-run
   within tolerance.
   **Status: shipped for the `shape` feature set** (2026-06-08). `clustering/model.py`
   (`build_shape_model`/`score_shape`, joblib-serialized centroids + scaler +
   novelty IsolationForest/LOF + vote thresholds + per-cluster verdict snapshot),
   tables `cluster_models`/`tx_classifications` (`005_models.sql`),
   `service.classify_new_transactions`/`update_contract`, a `kind=classify` job,
   and `POST /api/contracts/{target}/classify-new` (reached from the SPA via the
   `/api/clustering` proxy). The model is built lazily from the latest shape run
   on first use. **Not yet:** `graph` online scoring (MinHash signatures);
   cluster alignment across re-fits. The not-yet-classified **discovery query is
   O(history-within-window)** per call (full anti-join against `tx_classifications`);
   a future phase should bound it with a scored-watermark.

   **Drift detection shipped (2026-06-21).** The noise-rate drift signal from
   Part C is implemented: `classify_new_transactions` computes the trailing
   unassigned/noise rate (`Repo.online_noise_rate`), stored as
   `contracts.drift_score` (`008_contract_drift.sql`); the API derives
   `reclustering_suggested` against `RECLUSTER_NOISE_THRESHOLD` (default 0.25) and the
   SPA shows a "re-cluster recommended" badge with the job-detail note. This is the
   *trigger* half: it tells the operator *when* the frozen model is stale. Under the
   integrated feed it is also acted on automatically: a `done` contract whose
   `drift_score` crosses the threshold is re-fit on the next tick on a windowed
   population (the scheduler's `onboard` decision), so a drifting model is refreshed
   without operator action; a manual `process --reprocess` / "Re-analyze" remains
   available. The same run also decorrelated the online verdict (require both
   independent detectors to agree, noise flag excluded from `votes`) to cut the
   false-positive rate this drift signal would otherwise inflate; see
   [algorithms.md](algorithms.md).

   **Live verdict read-back (shipped 2026-06-09).** Online classifications are
   surfaced in the SPA's cluster/anomaly drill-down (the "Incoming" tab →
   `GET /api/contracts/{target}/classifications` →
   `service.online_classifications_with_verdicts`), with each tx's verdict
   **recomputed at read time** against its cluster's *current* label; the stored
   per-tx `verdict` is ignored. This resolves the old snapshot staleness: a manual
   **relabel now takes effect immediately** on incoming (and batch) members, with no
   model rebuild. The model's per-cluster verdict snapshot is therefore no longer
   load-bearing for display; a rebuild is needed only to refresh cluster *assignment*
   geometry (which legitimately requires a re-cluster), not verdicts. The read-back
   filters to the **current `model_id`**: superseded models' cluster ids are
   unresolvable (`cluster_models` keeps only the latest), so reading just the live
   model's rows is how the "rows span model versions" caveat is handled.
2. **Host-backed source + automatic feed.** **Shipped.** `HostChainSource`
   (`host_ch`) reads the host TMS's `tms_analytics` chain data; the feed
   scheduler (`service.scheduler`, `FEED_ENABLED`) auto-onboards and
   auto-classifies watched contracts as the host ingests their transactions, and
   `service.publish` writes the flagged verdicts to `tx_contract_anomaly` for the
   host to surface as the `contract_anomaly` attack class.
3. **Multi-worker + cluster alignment + MinHash graph.** Outstanding: the
   worker pool (Part D), cross-run cluster alignment, and online scoring for the
   `graph` feature set.

Each phase is independently shippable and observable.

## Open questions

- Tolerance for online-score vs batch-score divergence before forcing a re-fit?
- Per-contract or global re-fit policy (X, drift thresholds, reference-set
  sizes)?
- **Model-blob size.** A serialized `ShapeModel` is ~7 MB (mostly the pickled
  300-tree IsolationForest), stored base64 in ClickHouse (`tms_clustering`). At
  100 contracts × several models that's hundreds of MB and grows with every
  re-fit. Mitigate by lowering `n_estimators`, storing blobs off-DB (object
  store/disk), or pruning superseded models.
