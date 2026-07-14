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
  against the live-database test tier before shipping:
  `backend/tests/live_db/`, opt-in via `TMS_LIVE_DB_TESTS=1`.
