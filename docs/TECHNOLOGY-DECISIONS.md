# Architecture Decision Records

Technology and architecture decisions for the Cardano Transaction Monitoring System.

## ADR-001: ClickHouse as Analytics Warehouse

### Context

The system ingests all Cardano blockchain transactions into a queryable store. The data is append-only and time-series in nature. Key access patterns are: aggregate queries over large slot/time ranges, filtering by address or risk level, and batch reads by the Analysis Engine. Transaction volumes on Mainnet can reach hundreds per second.

### Decision

Use **ClickHouse** (MergeTree engine, deliberately unpartitioned; slot-ordered primary keys give the needed pruning at this scale, see `clickhouse_schema.py`) as the append-only **Analytics Warehouse** for `transactions`, `transaction_inputs`, `transaction_outputs`, and `tx_class_scores`. ClickHouse holds structured, normalized facts derived from the raw payloads in the Data Lake (filesystem). See ADR-009 for the Data Lake layer.

### Consequences

- Columnar storage compresses blockchain data aggressively (3–10× vs row-oriented stores).
- Analytical queries (COUNT, SUM, GROUP BY over millions of rows) execute an order of magnitude faster than on row-oriented databases.
- High-throughput batch inserts are the optimal write pattern; this matches the ingestion model.
- ClickHouse provides no transactional guarantees; lifecycle state requiring ACID semantics must be stored elsewhere (see ADR-002).
- Requires a separate process/container; adds operational overhead compared to SQLite.

### Alternatives Considered

| Alternative |
|---|
| PostgreSQL for everything |
| TimescaleDB |
| Elasticsearch |
| BigQuery / Snowflake |

## ADR-002: PostgreSQL as Admin Database

### Context

Transaction lifecycle management (PENDING → CONFIRMED → ROLLED_BACK) requires strict ACID guarantees: a transaction must atomically transition state exactly once, with a computed latency timestamp. Chain reorgs and concurrent chain sync / API writes require row-level locking and transactional consistency.

### Decision

Use **PostgreSQL** for the `tx_lifecycle` table, audit logs, API key configuration, and system metadata.

### Consequences

- Full ACID guarantees for lifecycle state transitions (no double-confirmation, no lost rollbacks).
- `asyncpg` provides a high-performance async connection pool that integrates cleanly with the FastAPI event loop.
- JSONB column for `raw_event` gives schema flexibility for raw mempool payloads.
- Row-oriented storage is the wrong fit for analytical queries; those remain in ClickHouse.
- Requires a separate container; PostgreSQL must not be used for OLAP-style workloads.

### Alternatives Considered

| Alternative |
|---|
| MySQL / MariaDB |
| SQLite |
| MongoDB |
| ClickHouse for lifecycle too |

## ADR-003: Sync Checkpoint and Entity State

### Context

The ingestion pipeline maintains two operational state objects: a **sync checkpoint** (last processed slot and block hash, used to resume ingestion after a restart without replaying the chain from origin) and **entity state** (per-address or per-policy metadata managed through the entities API). Both have simple key-value access patterns and low write throughput.

UTxO input-amount resolution is a related concern: confirming the ADA value consumed by each transaction input requires resolving the originating output from the UTxO set, which grows proportionally with chain history.

### Decision

Persist sync checkpoint and entity state in **PostgreSQL** (`sync_checkpoint` and `entity_state` tables). Two lightweight tables are sufficient at Preprod scale: a single-row UPSERT per block for the checkpoint, and a JSONB row per entity.

UTxO input-amount resolution is implemented **partially** using Ogmios `queryLedgerState/utxo`. When the mempool monitor observes a PENDING transaction, its inputs are guaranteed unspent (the node validates this before admitting the tx). A third WebSocket connection to Ogmios is used immediately after mempool observation to query the address and lovelace amount for each input. The results are cached in memory (`_pending_input_cache`) and applied to the `NormalizedTransaction` at ChainSync confirmation time (~20 s later), populating `total_input_value` and per-input `address` / `amount` in ClickHouse.

For transactions confirmed without prior mempool observation (i.e., submitted and confirmed faster than the mempool polling cycle), `total_input_value` remains `NULL`. A full historical UTxO index is not maintained.

`total_input_value` is stored as `Nullable(UInt64)`: `NULL` means unresolved, not zero.

### Consequences

- On restart, ingestion resumes from the saved slot via `findIntersection`; no replay from genesis.
- Sync checkpoint is a single-row UPSERT on `sync_checkpoint`; negligible cost at Preprod block rate (≈1 block per 20 s).
- `total_input_value` is populated for mempool-observed transactions; remains `NULL` for confirmed-only transactions. `NULL` means "input amounts unresolved", not "zero value".
- The UTxO query connection uses graceful degradation: failures reset the connection and return empty results, never blocking chain sync or mempool monitoring.
- The in-memory cache (`_pending_input_cache`) is not persisted across restarts; transactions in flight during a restart will have `NULL` `total_input_value`.
- A `/api/utxos` endpoint is not provided; resolved UTxO data is stored directly in ClickHouse `transaction_inputs`.

### Future Consideration: UTxO Resolution at Mainnet Scale

At Mainnet ingestion rates, the mempool-window approach scales with one round-trip per PENDING transaction. For high-throughput scenarios, batching multiple `queryLedgerState/utxo` calls (grouping inputs from multiple transactions into a single request) reduces round-trip overhead. The cache TTL for DROPPED transactions (currently unbounded at Preprod) should be bounded by `LIFECYCLE_PENDING_TTL_SECONDS` to prevent unbounded memory growth.

For historical UTxO resolution (transactions that bypass the mempool entirely), a fast key-value lookup against the live UTxO set would be required. PostgreSQL row lookups on a `(tx_hash, index)` index may be sufficient with a warm buffer cache; an embedded KV store (RocksDB or equivalent) is the recommended solution if PostgreSQL becomes a bottleneck.

## ADR-004: Ogmios as Cardano Node Bridge

### Context

The system needs two data streams from the Cardano node: (1) a streaming block feed to capture confirmed transactions in order, and (2) a mempool snapshot interface to detect pending transactions before block inclusion. The Cardano node communicates natively via Ouroboros mini-protocols encoded in CBOR over a Unix socket.

### Decision

Use **Ogmios v6** as the WebSocket bridge between TMS and the Cardano node. Ogmios exposes ChainSync (block streaming), LocalTxMonitor (mempool), and LocalStateQuery (`queryLedgerState/utxo` for UTxO input resolution) via JSON-RPC 2.0 over WebSocket.

### Consequences

- Structured JSON-RPC 2.0 over WebSocket is straightforward to implement in Python (`websockets` library).
- ChainSync provides blocks in strict on-chain order, which is required for correct `block_index` capture and UTxO state tracking.
- LocalTxMonitor provides mempool access unavailable through hosted APIs.
- LocalStateQuery (`queryLedgerState/utxo`) enables UTxO input resolution at the mempool observation window, the only point where inputs are guaranteed unspent. Three persistent WebSocket connections are maintained (chain, mempool, query); each uses a separate connection because the mini-protocols cannot be interleaved on one WebSocket.
- Ogmios must run co-located with the Cardano node (separate infrastructure concern).
- TMS connects to Ogmios via `OGMIOS_WS_URL` (default `ws://localhost:1337`).

### Version Compatibility

| Component | Version | Notes |
|---|---|---|
| Ogmios | **v6.14.0** | JSON-RPC 2.0 schema used throughout `ogmios_parser.py` and `ogmios_client.py`. Ogmios v7 changes the message envelope shape; upgrading requires re-validating all parser logic. |
| cardano-node | **11.0.1** | Required for the van Rossem PV11 hard fork; earlier 8.x/9.x/10.x nodes stall at the PV11 boundary. Test against the specific node version used in deployment before upgrading either component. |
| websockets (Python) | **≥16.0, <17.0** | Pinned minor series in `pyproject.toml`. The `websockets` 16.x API is stable; a major bump may change the `connect()` / `recv()` interface. |

**Upgrade procedure:** When the Cardano node is upgraded, check the Ogmios release notes for the matching Ogmios version. Update `OGMIOS_WS_URL`, bump the Ogmios deployment, then smoke-test all three mini-protocols before promoting to production:
- `ChainSync`: verify `nextBlock` responses parse correctly (`ogmios_parser.py`)
- `LocalTxMonitor`: verify `nextTransaction` returns expected fields
- `LocalStateQuery`: verify `queryLedgerState/utxo` with `outputReferences` returns `address` and `value.lovelace` for a known unspent output

The message shapes to validate are in `backend/app/ingestion/ogmios_parser.py` (parser) and `backend/app/ingestion/ogmios_client.py` (`_resolve_mempool_inputs`, `_query_utxo`).

### Alternatives Considered

| Alternative |
|---|
| Direct Ouroboros mini-protocols |
| Blockfrost API |
| Koios |
| cardano-graphql |

## ADR-005: FastAPI as Application Framework

### Context

The application has two concurrent I/O-heavy workloads: streaming data from Ogmios and serving REST/WebSocket API requests. Both require non-blocking async I/O. The system also needs auto-generated API documentation (Swagger UI) for operator and integrator use.

### Decision

Use **FastAPI** (on Uvicorn/ASGI) as the sole web framework, serving REST endpoints, WebSocket connections, the operator dashboard, and hosting the background ingestion tasks within a single `asyncio` event loop.

### Consequences

- Native `asyncio` support throughout; no thread pools needed for I/O operations.
- Automatic OpenAPI documentation (Swagger UI at `/docs`, ReDoc at `/redoc`) generated from Pydantic type annotations with zero extra configuration.
- Pydantic integration provides request validation, response serialization, and typed settings (`.env` parsing) in one library.
- Single-process deployment simplifies operations at Preprod scale.
- Running background tasks in the same process as the HTTP server means a task crash can affect API availability; this is an acceptable trade-off for Preprod.

### Alternatives Considered

| Alternative |
|---|
| Flask |
| Django |
| aiohttp |
| Tornado |

## ADR-006: Single-Process Architecture (Preprod)

### Context

The Preprod scope targets the Preprod testnet with low transaction volume and a single operator. The specification explicitly marks the Event Stream as optional at this stage. Deploying multiple networked services before the core pipeline is validated would add operational complexity with no immediate benefit.

### Decision

Run all four logical layers (Blockchain Connector, Analysis Engine, API Gateway, TMS Dashboard) as `asyncio` tasks within a **single Uvicorn process**. The Event Stream component is not deployed; the Blockchain Connector writes directly to storage.

### Consequences

- Single process, single port, single `docker-compose up`; minimal operational overhead.
- No inter-service serialization overhead.
- The in-memory `asyncio.Queue` used for internal event delivery maps directly to a message queue interface, making future decomposition straightforward.
- The in-memory rate limiter and WebSocket broadcast will not work correctly if the application is horizontally scaled; this is acceptable for Preprod but must be addressed before multi-instance deployment.
- A failure in the Blockchain Connector task can affect API availability (mitigated by the circuit breaker and task restart logic).

### Future

For Mainnet: Blockchain Connector → Event Stream (Kafka or Redpanda) → Analytics Warehouse consumers; Analysis Engine as an isolated worker pool; API Gateway as a standalone service.

### Alternatives Considered

| Alternative |
|---|
| Microservices from day one |
| Celery worker for Analysis Engine |

## ADR-008: Storage Assignment (ClickHouse vs PostgreSQL)

### Context

The system uses two databases (ADR-001, ADR-002). With two stores available, every piece of data requires an explicit placement decision. Without a clear governing principle, data ends up in whichever store was convenient at the time, leading to inconsistent access patterns, incorrect consistency expectations, and performance problems at scale.

### Decision

Apply a single governing principle to every placement decision:

> **ClickHouse stores immutable facts. PostgreSQL stores mutable state.**

A *fact* is something that happened on the Cardano blockchain and cannot change: a confirmed transaction, an analysis score, an input or output. A *state* is the current status of something that transitions over time and must be read with strong consistency.

### Property Comparison

| Property | ClickHouse | PostgreSQL |
|---|---|---|
| Storage model | Columnar; reads only the columns queried | Row-based; reads the entire row |
| Update model | Append-only; mutations at merge time, not immediately | Row-level UPDATE/DELETE, immediately consistent |
| Query strength | Scanning millions of rows, GROUP BY, aggregations | Single-row lookup by primary key, transactional upserts |
| Consistency | Eventually consistent (`FINAL` modifier for latest version) | Strong consistency; a read after a write always sees the write |
| Write pattern | High-throughput batch inserts | Low-volume, high-precision individual operations |
| Data lifetime | Unbounded time-series; grows forever | Bounded; one row per entity, represents current state |

### Per-Table Assignment

**ClickHouse: immutable facts**

| Table | Rationale |
|---|---|
| `transactions` | Append-only event log. Analytical queries scan millions of rows filtered by time, network, value. Columnar layout means `SELECT fee, risk_score WHERE timestamp > X` reads only 2 columns out of 17. |
| `transaction_inputs` | One row per input, never updated. Queried for graph analytics and address tracing. |
| `transaction_outputs` | Same. Queried for address lookups and value analytics. |
| `address_transactions` | Populated by materialized view. `ReplacingMergeTree ORDER BY (network, address, slot, tx_hash)` gives O(log n) address lookup with no application code. |
| `tx_class_scores` | Analysis output. `ReplacingMergeTree(analyzed_at)` handles re-scoring: the engine inserts a new row and the engine's dedup key retains the latest version at merge time. |

**PostgreSQL: mutable state**

| Table | Rationale |
|---|---|
| `tx_lifecycle` | A transaction moves through states (PENDING → CONFIRMED / ROLLED_BACK / DROPPED). This is a single row per `tx_id` that is updated in-place. `SELECT status WHERE tx_id = ?` must return `CONFIRMED` immediately after a block confirmation; not after the next ClickHouse background merge. |
| `sync_checkpoint` | One row per network. Updated on every block. Must be durably committed before the next block is processed. A crash between the ClickHouse insert and the checkpoint save causes the block to be replayed on restart; ClickHouse MergeTree handles the duplicate safely. |
| `entity_state` | User-defined key-value annotations. Values are overwritten with `PUT`. Needs immediate read-your-writes consistency. |

### The Lifecycle Split: Most Important Consequence

The same transaction exists in both databases serving different purposes:

```
tx_lifecycle (PostgreSQL)           transactions (ClickHouse)
─────────────────────────           ─────────────────────────
Tracks current STATUS               Records the immutable FACT

Written at PENDING (mempool)        Written at CONFIRMED (chain)
Updated at CONFIRMED                Never updated
Updated at ROLLED_BACK
Updated at DROPPED

Answers: "Is this tx confirmed?"    Answers: "Show me all txs in last hour"
         "How long did it wait?"             "What is the average fee?"
         "Was it rolled back?"               "Which addresses are high-risk?"
```

If `tx_lifecycle` were in ClickHouse, every status check would require `SELECT ... FINAL` (expensive full-merge scan), and between a confirmation event and the next background merge, the status would still show `PENDING`. For a real-time monitoring dashboard this inconsistency window is unacceptable. PostgreSQL row-level UPDATE gives immediate, strongly-consistent status reads.

### Consequences

- Every new table added to the system is evaluated against the governing principle before placement, preventing ad-hoc decisions.
- Analytical queries (risk scoring, address clustering, time-series) always run against ClickHouse; they do not compete with lifecycle writes for PostgreSQL connections.
- Status queries (is this tx confirmed?) always run against PostgreSQL; they do not wait for ClickHouse merges.
- A ROLLED_BACK transaction leaves a row in ClickHouse `transactions` permanently. The lifecycle API (PostgreSQL) is the source of truth for current status. Queries that need both the fact and the status must join across both stores at the application layer.

## ADR-009: Local Filesystem for Raw Transaction Storage (Preprod)

### Context

Every Cardano transaction carries a full Ogmios JSON payload (2–600 KB, larger for Plutus scripts). Before this ADR, the raw payload was stored in `tx_lifecycle.raw_event` (PostgreSQL JSONB), written once at PENDING, **never read** by any API endpoint, analysis query, or dashboard. PostgreSQL TOAST stores columns exceeding ~2 KB off-page, adding hidden I/O overhead on every `tx_lifecycle` row scan even though `raw_event` is never selected.

The ClickHouse `raw_data` column (64 KB truncated debug snippet) is a separate concern and is kept as-is.

### Decision

Store full raw transaction payloads as **gzip-compressed JSON files on the local filesystem**. This layer is the system's **Data Lake**: write-once, schema-on-read, complete raw payloads. The Analytics Warehouse (ClickHouse) is a derived, structured view over this raw data; if ClickHouse is wiped, it can be reconstructed by replaying the Data Lake files.

Path structure:
```
{RAW_STORE_PATH}/{prefix}/{network}/{YYYYMMDD}/{tx_hash[:2]}/{tx_hash}.json.gz
```

The `{tx_hash[:2]}` shard directory is the first 2 hex characters of the transaction hash (256 uniform buckets). Cardano tx_hashes are SHA-256 derived, so the first bytes are uniformly distributed. At Mainnet scale (~3M txs/day) this limits each leaf directory to ~11,700 files, within the efficient range for ext4, XFS, and APFS. For S3/MinIO the shard prefix distributes PUT operations across multiple index partitions, avoiding the hot-prefix throttling that occurs with millions of sequential keys under the same prefix.

| Prefix | Written by | Content |
|---|---|---|
| `confirmed/` | Chain sync, after block confirmation | Full Ogmios transaction JSON |
| `mempool/` | Mempool monitor, first observation | Full Ogmios transaction JSON |

- Async writes via a dedicated 2-worker `ThreadPoolExecutor` (same pattern as ClickHouse).
- Atomic writes: gzip to `{tx_hash}.json.gz.tmp`, then `os.replace()` (POSIX `rename(2)`). A crash mid-write leaves only the `.tmp` file; the final path does not exist, so the write is retried safely on replay.
- Write-once: if the final path already exists, the write is skipped (idempotent on restart replay).
- Feature-flagged via `RAW_STORE_ENABLED` (default `true`).
- `raw_event JSONB` column dropped from `tx_lifecycle`.

### Upgrade Path

| Scale | Storage | Notes |
|---|---|---|
| Preprod | Local filesystem (this ADR) | Zero new service; Docker named volume |
| Production / multi-instance | **MinIO** (S3-compatible) | Drop-in `boto3` client; same path structure; single Docker container |
| Mainnet | Cloudflare R2, Backblaze B2, or AWS S3 | Same `boto3` client; change `S3_ENDPOINT_URL` only |

When moving to MinIO: replace `_write_sync` / `read_raw` in `backend/app/db/raw_store.py` with `boto3` S3 calls. The path structure (`{prefix}/{network}/{YYYYMMDD}/{shard}/{tx_hash}.json.gz`) maps directly to S3 key prefixes; the shard component improves S3 write throughput by distributing keys across internal index partitions.

### Alternatives Considered

| Alternative | Reason rejected |
|---|---|
| PostgreSQL JSONB (`raw_event`) | Write-only; TOAST overhead; not queryable for analytics |
| ClickHouse String column (full blob) | 300 KB+ blobs degrade compression; wrong tool for blob storage |
| RocksDB | Optimised for small KV lookups (<10 KB); large Plutus blobs cause write amplification; Python binding removed (ADR-003) |
| MinIO at Preprod | Extra Docker container for Preprod with <100 txs/day; unjustified overhead |

### Consequences

- No raw transaction data is lost: `confirmed/` captures every on-chain transaction; `mempool/` captures first observation.
- PostgreSQL `tx_lifecycle` rows become lighter: no TOAST column, faster row scans for lifecycle queries.
- Raw blobs are not queryable via SQL; access is by `(prefix, network, date, tx_hash)` tuple only.
- A ROLLED_BACK transaction leaves a file in `confirmed/`; the lifecycle API (PostgreSQL) is the source of truth for current status.

## ADR-007: Python as Implementation Language

### Context

The system spans data ingestion, storage integration, REST API serving, graph analysis (`analysis/graph.py`), and the clustering sidecar (`services/clustering/`). The primary constraint is ecosystem availability for both Cardano tooling and data science libraries.

### Decision

Implement the full backend in **Python 3.13+**.

### Consequences

- Strong ecosystem support: `websockets`, `asyncpg`, `clickhouse-driver`, `fastapi`, `pydantic`; all well-maintained async Python libraries.
- The clustering and graph-analysis stack (scikit-learn, NetworkX, pandas; shipped in `services/clustering/` and `analysis/graph.py`) is native to the Python data science ecosystem.
- No unusual runtime constraints at Preprod scale.
- Python's GIL limits CPU-bound parallelism; this is not a concern for the I/O-bound ingestion pipeline but may become one for compute-intensive clustering at Mainnet scale.

### Trade-off Acknowledged

For very high-throughput Mainnet ingestion, a lower-level language (Go, Rust) would provide better raw throughput in the Blockchain Connector. This is a viable future optimization that does not require changes to the storage or API layers.

### Alternatives Considered

| Alternative |
|---|
| Go |
| Rust |
| Node.js |
