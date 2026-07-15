# Data Model (ClickHouse)

The clustering module owns its own state in the **`tms_clustering`** database, a
sibling of the TMS's `tms_analytics` database on the **same** ClickHouse server.
Two databases, one server: the module never stands up its own ClickHouse.

Raw transaction and feature data is **read** from `tms_analytics` (the same
Ogmios-ingested chain data the core scorers use) via `HostBackedRepo`, selected
by `CHAIN_SOURCE=host_ch`. It is **not** stored in `tms_clustering`. Everything
this module produces (clusters, anomaly runs, models, classifications, and the
`tx_contract_anomaly` projection the host reads back) lives in `tms_clustering`.

All SQL lives in the
[storage/clickhouse/](../backend/app/storage/clickhouse/) package (`ClickHouseRepo`,
composed from per-entity mixins, plus `HostBackedRepo` for the cross-database
feature reads); schema in [clickhouse/init/](../clickhouse/init/)
(`001_schema.sql`, `002_anomaly.sql`, `003_contracts_jobs.sql`, `004_tx_labels.sql`,
`005_models.sql`, `006_cursor.sql`, `007_run_origin.sql`, `008_contract_drift.sql`,
`009_contract_anomaly.sql`). The `tms` database token in those files is rewritten
to `tms_clustering` by `python -m app.cli migrate` (see [Migrations](#migrations)).

## Tables

### Raw transaction / feature data: read from `tms_analytics`

The shape and graph feature builders consume per-transaction facts (input/output
counts, lovelace totals, sizes, asset moves, redeemer counts) that the host TMS
has **already ingested** from the chain. `HostBackedRepo` reads them directly
from `tms_analytics` with fully-qualified cross-database queries, bridging three
host-side gaps at read time so the module's feature builders see exactly their
expected column contract:

- `size` comes from the additive host column `transactions.tx_size_bytes`.
- `distinct_assets` is computed from the inputs/outputs `assets` JSON.
- `redeemer_count` is a LEFT JOIN to `tx_script_features`, coalesced to `0` for
  non-script txs.

A watched contract is an address `target`. In the default `host_ch` mode its
transactions are resolved from the host's `address_transactions` and bounded to the
most recent `CLUSTERING_WINDOW_TXS` so DBSCAN / Isolation Forest and the O(n²)
silhouette stay bounded on a high-volume mainnet contract. Because the host already
ingested the chain, in that mode there is no external download and no second copy of
the chain data.

The module's schema defines `transactions`, `tx_utxos`, `tx_utxo_assets`, and
`ingest_cursor` in `tms_clustering` (they describe the column contract the feature
builders expect). Under `host_ch` they are **never populated**: writes to them and to
the ingest cursor are no-ops, since the sidecar reads from `tms_analytics` rather than
downloading anything. Under `CHAIN_SOURCE=blockfrost` they **are** populated: the
download path fetches each transaction from blockfrost.io and writes it here, and the
feature builders read from these tables instead of `tms_analytics`.

| Table | Engine | ORDER BY | Purpose |
|---|---|---|---|
| `transactions` | `ReplacingMergeTree(ingested_at)` | `(target, tx_hash)` | Per-tx shape facts. Under `host_ch` read from `tms_analytics` and not populated here; under `blockfrost` populated by the download path. |
| `tx_utxos` | `ReplacingMergeTree` | `(target, tx_hash, role, idx, address)` | Column contract for per input/output UTXO graph features. |
| `tx_utxo_assets` | `ReplacingMergeTree` | `(target, tx_hash, role, idx, unit)` | Column contract for native assets moved. |
| `ingest_cursor` | `ReplacingMergeTree(updated_at)` | `(target)` | Resume cursor for the column contract; inert under `host_ch` (no ingestion here), used by the `blockfrost` download path to resume discovery. |

### Derived results

| Table | Engine | ORDER BY | Purpose |
|---|---|---|---|
| `cluster_runs` | `ReplacingMergeTree(created_at)` | `(run_id)` | One row per DBSCAN run (params, counts, silhouette, `origin` = system\|custom). |
| `cluster_labels` | `ReplacingMergeTree` | `(run_id, cluster_id, tx_hash)` | Per-tx cluster assignment (`-1` = noise). |
| `anomaly_runs` | `ReplacingMergeTree(created_at)` | `(run_id)` | One row per anomaly run (methods, eps/min_samples, counts). |
| `anomaly_scores` | `ReplacingMergeTree` | `(run_id, score_rank, tx_hash)` | Per-tx ensemble scores. `iso_score` is `NaN` for the precomputed metric. |

### Contract-anomaly projection (`009_contract_anomaly.sql`)

This is the projection the **host** reads back. The scheduler classifies each
watched contract's new transactions against its frozen model and publishes the
per-`(contract, transaction)` verdicts here; the host TMS reads this table at API
time and surfaces them as the `contract_anomaly` attack class through
`/api/analysis/results`.

| Table | Engine | ORDER BY | Purpose |
|---|---|---|---|
| `tx_contract_anomaly` | `ReplacingMergeTree(published_at)` | `(network, tx_hash, target)` | Per-(watched-contract, transaction) anomaly verdict the host reads back as `contract_anomaly`. |

Columns:

| Column | Type | Meaning |
|---|---|---|
| `network` | `String` | Cardano network the tx is on. |
| `tx_hash` | `String` | The classified transaction. |
| `target` | `String` | The watched contract this verdict is for. |
| `cluster_id` | `Int32` | Frozen cluster the tx classified into; `-1` = online noise / unassigned. |
| `iso_score` | `Float64` | Isolation Forest score (evidence). |
| `lof_score` | `Float64` | Local Outlier Factor score (evidence). |
| `consensus` | `Float64` | `[0,1]` ensemble consensus (NaN-safe). |
| `votes` | `UInt8` | `0..3` detector votes. |
| `verdict` | `String` | `malicious` \| `benign` \| `anomaly` \| `normal`. |
| `model_id` | `String` | The frozen `ShapeModel` that scored it. |
| `feature_set` | `String` | `shape` \| `graph` \| `combined`. |
| `evidence` | `String` (JSON, default `{}`) | Top deviating features, etc. |
| `scored_at` | `DateTime` (default `now()`) | SOURCE time: when the run/classify that produced the verdict happened. |
| `published_at` | `DateTime64(6)` (default `now64(6)`) | RECONCILIATION version: the `ReplacingMergeTree` version column, stamped monotonically on every publish, relabel, clear, and delete. |

`tx_contract_anomaly` notes:

- **Stores the raw detector outputs** (verdict / consensus / votes / detector
  scores), deliberately **not** a host-scale 0-100 score. The host computes the
  score from these via its `contract_anomaly` projection config, so the mapping
  has a single source of truth. Recall-first calibration of that mapping stays on
  the host side where the rest of the attack classes are scored.
- **Keyed by `(network, tx_hash, target)`** because one transaction can be touched
  by several watched contracts; the host read-merge collapses to the
  highest-severity verdict for a given tx.
- `ReplacingMergeTree(published_at)`, not `(scored_at)`, keeps the latest
  *reconciliation* per key: versioning on `scored_at` would let an older
  tombstone keep beating a re-published positive after a benign label is
  cleared, hiding the re-raised alert until a future fit produced a newer
  `scored_at` (see `clickhouse/init/009_contract_anomaly.sql`). A
  re-classification (after a re-cluster) still supersedes the stale row on the
  next merge.

### Manual verdict labels (`004_tx_labels.sql`)

| Table | Engine | ORDER BY | Purpose |
|---|---|---|---|
| `tx_labels` | `ReplacingMergeTree(updated_at)` | `(target, tx_hash)` | Manual `malicious`/`benign` verdicts a user applies to a cluster's members. |

`tx_labels` notes:

- Keyed on the **stable `tx_hash`**, *not* `(run_id, cluster_id)`: cluster ids are
  ephemeral per run, so per-tx labels are what survive reprocessing. "Labelling a
  cluster" writes one row per current member; future txs that cluster alongside a
  labelled one inherit the verdict at display time (`service.compute_verdicts`).
- **Clearing is a tombstone**, not a delete: `clear_tx_labels` inserts a row with
  `deleted = 1` (append-only, like the rest of the schema; no async `ALTER … DELETE`).
  All reads use `FINAL` and filter `deleted = 0`.
- Effective per-tx verdict precedence (highest first): explicit per-tx label →
  cluster-inherited label → auto-anomaly (`votes >= 2`). The noise bucket (`-1`)
  never propagates inheritance.

### Registry & jobs (`003_contracts_jobs.sql`)

| Table | Engine | ORDER BY | Purpose |
|---|---|---|---|
| `contracts` | `ReplacingMergeTree(updated_at)` | `(target)` | Watched-contract registry: identity/metadata + status. |
| `jobs` | `ReplacingMergeTree(updated_at)` | `(job_id)` | Onboarding/refresh job tracking for UI progress polling. |

`contracts` columns of note:

- `present UInt8`: existence flag. **Named `present`, not `exists`**, to avoid the
  ClickHouse `EXISTS` keyword; the API/UI expose it as **`exists`**. The repo maps
  `exists ↔ present` in `save_contract` / `_contract_row_to_dict`.
- `balance_lovelace Int128`: ADA balance (0 for policy targets).
- `script_type String`: `plutusV1/V2/V3`, `timelock`/native, or `''`.
- `sample_tokens String`: JSON array of `{unit, policy_id, name}`.
- `tx_count UInt32`: **snapshot** written by the pipeline (see "tx_count" below).
- `status Enum`: `pending | processing | done | failed`.
- `drift_score Float64` (`008_contract_drift.sql`): trailing **online-noise rate**
  (fraction of recently classified txs unassigned to any frozen cluster) written by
  `update_contract`. The API derives `reclustering_suggested` from it vs
  `RECLUSTER_NOISE_THRESHOLD`. A batch `process_contract` save resets it to `0`
  (a fresh re-cluster supersedes the stale model). See
  [algorithms.md](algorithms.md) and [online-classification-design.md](online-classification-design.md).

`jobs` columns of note:

- `status Enum`: `queued | checking | downloading | clustering | scoring | done | failed`
  (the same stage names `process_contract` emits).
- `max_txs UInt32` / `requested_max_txs UInt32`: `0` means **unbounded**. In the
  integrated deployment the windowing is applied at read time against `tms_analytics`
  (`CLUSTERING_WINDOW_TXS`), so a contract is classified over its most recent
  transactions rather than a separately-downloaded slice.
- `created_at`, `updated_at`: **`DateTime64(6)`** (microsecond), see below.

## `ReplacingMergeTree` + `FINAL`

These tables keep the **latest** row per ORDER-BY key, deduplicated by the version
column (the `*_at` timestamp) at merge time. Background merges are asynchronous, so
**every read uses `FINAL`** to force dedup-on-read and get correct results
immediately.

> ⚠️ **`FINAL` on the run / projection tables is load-bearing, not optional.**
> Reprocessing is idempotent *because* re-inserting a row (after a re-cluster, a
> resume, or a republish) is collapsed by `FINAL`. Do **not** drop `FINAL` from
> these reads: it would double-count duplicate rows. The same applies to the
> `transactions` / `tx_utxos` column-contract tables if they are ever exercised
> outside the integrated deployment.

`count(DISTINCT tx_hash)` (in `list_targets`) intentionally omits `FINAL`: `DISTINCT`
already collapses duplicate `(target, tx_hash)` rows for counting.

## Job status durability (`update_job`)

`update_job` is a **read-latest → merge → re-insert** of the full row, omitting
`updated_at` so the server stamps a fresh `now64(6)`; the `ReplacingMergeTree` then
keeps that newest row. `created_at` is read and preserved across updates.

Two safeguards make this race-free:

1. **Single writer per job**: only the one JobManager worker thread updates a job
   (documented at `update_job`). Any future concurrent writer would need a different
   strategy.
2. **Microsecond timestamps**: `DateTime64(6)` plus the network round-trip between
   successive writes makes version ties between rapid stage updates effectively
   impossible (a millisecond `DateTime64(3)` could tie and let a stale status win).

## The `tx_count` snapshot

`list_contracts` / `get_contract` read the **stored** `contracts.tx_count` (written
by `process_contract`) rather than re-scanning on every list call. Trade-off: a
`pending`/`processing` contract (including during a reprocess) shows `tx_count = 0`
until the job finishes. This is intentional: it keeps the contract list O(1) per row
instead of a full scan.

## Row mapping

Result rows are mapped **by name**, not by position, via the module helper
`_row_to_dict(keys, row, *, int_keys, float_keys, nan_none_keys)`. Each entity has a
single source of truth: a SELECT-column constant (`_RUN_SELECT`, `_CONTRACT_SELECT`,
`_JOB_SELECT`, `_ANOMALY_RUN_SELECT`) and an output-key spec whose order matches the
SELECT. The `strict=True` zip fails fast if a projection and its key list drift.

## Migrations

The `tms_clustering` schema is created idempotently from `clickhouse/init/*.sql`.
The `tms` database token in those files is rewritten to the configured database
(`CLICKHOUSE_DB=tms_clustering`) at apply time, so the same init scripts produce
the sibling database on the host's ClickHouse server.

On a fresh data volume the ClickHouse entrypoint would auto-load the scripts
(`docker-entrypoint-initdb.d`). In the integrated deployment, where the database
sits on the host's existing server, run the migrate command. It re-applies every
init file in name order, and the convention is that **every statement is
self-idempotent** (`CREATE … IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, guarded
`UPDATE`), so re-running is always safe:

```bash
docker exec tms-clustering-sidecar python -m app.cli migrate
```

The backend **fails fast at startup** when the live schema is behind the code
(missing tables/columns it knows it needs), with a message naming exactly
what's missing and the command above. So the upgrade procedure is simply:
pull → bring the stack up → if the sidecar reports schema drift, run migrate → it
restarts clean (`restart: unless-stopped` retries it).

When adding a migration: create the next `clickhouse/init/NNN_*.sql`, keep every
statement idempotent, never put `--` inside a string literal (the runner strips
`--` comments before splitting statements), and extend `_EXPECTED_TABLES` /
`_EXPECTED_COLUMNS` in `storage/clickhouse/base.py` so the startup guard
enforces it.

All `CREATE TABLE` statements use `IF NOT EXISTS`, so re-applying is safe. There is
no migration framework: the convention is small, ordered, idempotent `.sql` files
applied by `migrate`, plus hand-applied `ALTER`s where a live volume needs them.

## Growth / maintenance

Reprocessing and republishing append duplicate parts that `FINAL` dedups on read but
that accumulate on disk until a merge. At showcase scale this is negligible. For
sustained use, run an occasional `OPTIMIZE TABLE tms_clustering.<table> FINAL` after
large re-cluster batches (or add a TTL on the run tables) to bound part count and
`FINAL` cost. Under `host_ch`, raw chain data is **not** stored here, so the on-disk
footprint of `tms_clustering` is just clusters, runs, models, labels, and the
`tx_contract_anomaly` projection, not a copy of the chain. Under `blockfrost` it also
holds the downloaded transactions / UTXOs / assets for each onboarded address.
