# Host boundary: how the module and the TMS host share ClickHouse

The clustering module runs beside the host TMS on the same ClickHouse server
but touches it through a deliberately narrow seam. This page documents that
seam: the two-driver decision, the column vocabulary the sidecar reads from the
host, the modules that exist as paired copies, and the ClickHouse 26.x
behaviours that bite exactly at this boundary.

Path convention on this page: `backend/...` paths are the host TMS backend at
the repository root; `app/...` paths are this module's own backend
(`services/clustering/backend/app/...`).

## Two ClickHouse drivers: frozen by decision

The host and the sidecar talk to the same ClickHouse server with different
drivers, and this is intentional:

| | Host TMS | Clustering sidecar |
|---|---|---|
| Driver | `clickhouse-driver` (native protocol) | `clickhouse-connect` (HTTP) |
| Port | 9000 | 8123 |
| Connection code | `backend/app/db/clickhouse.py` | `app/storage/clickhouse/base.py` (`connect()`) |
| Database | `tms_analytics` (chain facts) | `tms_clustering` (module state), reads `tms_analytics` via `HostBackedRepo` |

Migrating both onto one driver was considered and rejected. Each driver is
load-bearing where it sits: the host depends on the native protocol's
streaming inserts and its thread-local client + executor pattern, while the
sidecar depends on `clickhouse-connect`'s `query_df` frames and stateless HTTP
requests. A swap on either side would churn tested timeout and retry semantics
for no functional gain. `clickhouse-connect` is the choice for NEW modules.

The drivers also give the same timeout name different meanings, which is why
both services read the same env name but need different values:

- `CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS` on the host (native protocol)
  bounds the idle time between packets of a streaming response; 120 seconds
  bounds a wedged socket well below the driver default without touching a
  legitimately heavy streaming query.
- The same variable on the sidecar (HTTP) spans the whole query, because HTTP
  returns no bytes until the query finishes; a long legitimate fit over the
  full window needs the 300 second default.
- `CLICKHOUSE_CONNECT_TIMEOUT_SECONDS` (10 seconds on both) caps TCP
  connection establishment so a partition without an RST fails fast.

Because the env names collide, the root `docker-compose.yml` gives the sidecar
its own per-service knobs, `CLUSTERING_CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS`
and `CLUSTERING_CLICKHOUSE_CONNECT_TIMEOUT_SECONDS`, which it maps onto the
shared names inside the container; the unprefixed variables keep configuring
the host only.

## Column vocabulary mapping

The host warehouse and the engine use different column vocabularies for the
same transaction facts. `HostBackedRepo`
(`app/storage/clickhouse/host_backed.py`) is the single bridge: it
projects the host's columns into the engine's contract at read time, so no
other module ever sees a host column name.

| Host (`tms_analytics`) | Engine contract | Bridge |
|---|---|---|
| `transactions.fee` | `fees` | rename |
| `transactions.timestamp` (`DateTime`) | `block_time` | rename |
| `transactions.total_output_value` | `total_output_lovelace` | rename |
| `transactions.total_input_value` | `total_input_lovelace` | rename, `ifNull(..., 0)` |
| `transactions.tx_size_bytes` | `size` | rename (0 for rows ingested before the host column existed) |
| `transactions.tx_hash` (`String`) | `tx_hash` (`FixedString(64)` in the sidecar's own tables) | `toString(...)` on every cross-database projection and comparison; a raw `FixedString` vs `String` comparison is padding-sensitive |
| derived | `net_lovelace` | `CAST(total_output_value AS Int64) - CAST(ifNull(total_input_value, 0) AS Int64)` |
| derived | `distinct_assets` | `uniqExact` over the inputs'/outputs' `assets` JSON keys |
| `tx_script_features.redeemers_count` | `redeemer_count` | LEFT JOIN, unmatched (non-script) side coalesced to 0 |

The extension point for this bridge is the trio of tx-source hooks on the base
repository (`_tx_relation`, `_tx_hashes_relation`, `_tx_scope_params` in
`app/storage/clickhouse/base.py`). The five transaction-joined reads
(`latest_transactions`, `unclassified_tx_hashes`, `top_anomalies`,
`cluster_summary`, `cluster_transactions`) are written once in their mixins
against those hooks; `HostBackedRepo` overrides only the hooks. A new read
that joins transaction context should be written the same way, against the
hooks, and never against a concrete table.

## Paired-copy modules

The host and sidecar packages deliberately cannot import each other (separate
build contexts, separate images), so one module exists as a paired copy:

- `backend/app/utils/bech32.py` (host: validating core plus the scorers'
  lenient wrapper)
- `services/clustering/backend/app/registry/bech32.py` (sidecar: the same
  validating core, decode path only)

The validating decode core must stay textually in sync; each file's docstring
names its twin. When editing one, mirror the change in the other within the
same reviewed change.

## ClickHouse 26.x gotchas

Behaviours observed live on ClickHouse 26.x that mocked tests cannot catch:

- An aggregate alias must not shadow a source-column name used by sibling
  aggregates (error Code 184). This is why `cluster_summary` aliases
  `count() AS cluster_size` and never `AS size`: the tx relation projects a
  `size` column into the join input that the sibling `avg(...)` aggregates
  read.
- Table-level settings gate projected `CREATE`/`DELETE`: a table carrying a
  projection can reject DDL and lightweight deletes that pass against a plain
  table.
- Because fakes never execute SQL, validate any DDL or query-text change
  against a live-database test tier before shipping. This module's own tier is
  `services/clustering/backend/tests/live_db/` (it executes the history/hybrid
  query text); host-side changes belong in the host's `backend/tests/live_db/`.
  Both opt in via `TMS_LIVE_DB_TESTS=1` and run in CI's Live-DB job.

## The history boundary and the host backfill trigger

Two invariants added with the pre-deployment history backfill
(`service/history.py`) sit exactly on this host boundary:

**The immutability boundary.** The host's chain-rollback purge
(`_handle_roll_backward` + `delete_clustering_rows`) deletes orphaned rows from
the HOST tables and from the module's `tx_contract_anomaly` and
`tx_classifications`, but never from the module's raw tables: nothing else ever
wrote them under host_ch. A backfilled row near the tip could therefore become
a fork ghost that re-enters every fit with no purge path. The backfill instead
persists only rows strictly below
`least(target's earliest host slot, host tip - ROLLBACK_SAFETY_SLOTS)`, with
`ROLLBACK_SAFETY_SLOTS = 129600` (Cardano's stability window `3k/f`, about 36
hours on mainnet) and the block-height twin `ROLLBACK_SAFETY_BLOCKS = 6480`
(`3k`: the block count of that same 36-hour span at the active-slot rate
`f = 0.05`; the security parameter `k = 2160` alone would span only a third of
it and let the bounded walk fetch rows the slot guard then drops, burning the
per-contract cap). History below that line is immutable by protocol,
so the missing purge path is harmless, and the local rows are provably disjoint
from the host rows, which the hybrid reads and the publish filter lean on.

**The publish bound.** The host's contract_anomaly notifications poller alerts
on EVERY flagged `tx_contract_anomaly` row, builds the alert purely from the
verdict row, and links to `/attacks/{tx_hash}`: it has no host-membership
check. A verdict published for a backfilled-history tx the host never ingested
would page an operator to a page the host cannot render. Publishing therefore
intersects the flagged set with what the HOST actually knows
(`host_known_tx_hashes`, a query against the host's `address_transactions`),
so a host-known transaction is never suppressed (recall first, even against
stale local rows an earlier blockfrost-primary run left in the module's own
tables) and a host-unknown one never leaks into the projection (regardless of
the current `HISTORY_SOURCE` setting). History verdicts stay fully visible
inside the module's own UI reads. Pure host_ch and kupo deployments pass
through unchanged: every classified transaction is already a host row.

**The kupo trigger contract.** `HISTORY_SOURCE=kupo` calls the host's
`POST /api/v1/backfill` with `{address, max_txs, created_before_slot}` and the
`X-API-Key` header (`HOST_API_URL`/`HOST_API_KEY`), trigger-and-continue: 202
and 409 both mean "running" (a marker row in `ingest_cursor`, `source='kupo'`,
tracks it; later classify ticks poll `GET /api/v1/backfill/{address}` and flip
the marker), 503 means the host has no `KUPO_URL` and the attempt is deferred.
Re-triggers are bounded (`_KUPO_MAX_TRIGGERS`); exhausting the budget settles a
gave-up marker surfaced as `history_status: "failed"`, and raising the
per-contract cap re-opens it with a fresh budget. Auth failures from the host
(401/403, a wrong `HOST_API_KEY`) are warned in the logs by name: the startup
guard verifies the key is set, not that the host accepts it.
`created_before_slot` is the boundary above, so the host walks pre-deployment
history instead of re-covering what it already ingested. Two accepted,
deliberate consequences of this flavor: the host detection engine ALSO scores
the backfilled rows (fresh `ingestion_timestamp`) and can fire immediate alerts
for old transactions, and host rows persist if the watched contract is later
deleted from the module (the module's delete purges only its own tables).
