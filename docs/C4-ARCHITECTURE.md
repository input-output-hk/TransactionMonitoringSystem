# C4 Architecture: Cardano Transaction Monitoring System

## Level 1: System Context

Who uses the system and what external systems does it interact with.

```mermaid
C4Context
    title System Context: Cardano Transaction Monitoring System

    Person(user, "User", "Monitors, queries, and investigates Cardano transactions via dashboard and REST API")

    System(tms, "Transaction Monitoring System", "Ingests Cardano blockchain data in real time. Tracks transaction lifecycle (PENDING → CONFIRMED → ROLLED_BACK). Provides the 9-class Polimi detection engine for risk scoring, plus an optional first-party clustering / unsupervised-anomaly module (the contract_anomaly attack class) that profiles watched contracts.")

    System_Ext(cardano_node, "Cardano Node", "Full node: source of truth for chain state, mempool, and block data")
    System_Ext(ogmios, "Ogmios v6", "WebSocket bridge to Cardano node: provides ChainSync, LocalTxMonitor, and LocalStateQuery mini-protocols via JSON-RPC 2.0")
    System_Ext(mail, "SMTP Mail Relay", "Delivers magic-link login emails (mailpit in dev, real relay in production)")

    Rel(user, tms, "Browse and investigate; magic-link login", "HTTPS, WebSocket")
    Rel(tms, ogmios, "Blocks + mempool + UTxO queries", "WebSocket :1337")
    Rel(tms, mail, "Send magic-link emails", "SMTP")
    Rel(ogmios, cardano_node, "Chain state", "Node-to-Client IPC")
```

## Level 2: Container Diagram

The four layers from the spec mapped to running containers.
Event Stream is optional for Preprod; the Blockchain Connector writes directly to storage.

```mermaid
C4Container
    title Container Diagram: Transaction Monitoring System

    Person(user, "User")

    System_Boundary(tms, "Transaction Monitoring System") {
        Container(connector, "Blockchain Connector", "Python, websockets", "Ingestion layer. Three persistent Ogmios WebSocket connections: ChainSync, LocalTxMonitor, LocalStateQuery. Normalizes transactions. Resolves mempool input UTxOs. Circuit breaker + checkpoint resumption.")
        Container(api, "API Gateway", "FastAPI, Uvicorn", "Services layer. REST and WebSocket endpoints. Dual auth: TMS-API-Key (programmatic) and magic-link session cookies (dashboard, Admin/Reviewer roles). Per-key/IP rate limiting. User-management + archive APIs.")
        Container(analysis, "Analysis Engine", "Python", "Services layer. 9-class Polimi detection engine. Reads unscored transactions, assigns multi-class risk scores, writes results to Analytics Warehouse.")
        Container(ui, "TMS Dashboard", "React SPA (served by API Gateway)", "Presentation layer. Real-time mempool feed, confirmed txs, lifecycle stats, analysis results, and the Validators / cluster-graph views.")
        Container(clustering, "Clustering Module", "Python, FastAPI, scikit-learn", "OPTIONAL first-party sidecar (services/clustering, --profile clustering). Per-contract DBSCAN clustering + IsolationForest/LOF anomaly detection. Publishes the contract_anomaly verdict the API merges in. Reads chain facts from the Analytics Warehouse; owns its own ClickHouse database.")
        ContainerDb(datalake, "Analytics Warehouse", "ClickHouse MergeTree", "Storage layer. Structured blockchain facts: transactions, inputs, outputs, analysis results. Append-only columnar store. Derived from the Data Lake.")
        ContainerDb(clusterdb, "Clustering State", "ClickHouse (tms_clustering DB, same server)", "Storage layer. The clustering module's own state: cluster_models, tx_classifications, and tx_contract_anomaly (the projection the host reads). Empty/absent unless the module runs.")
        ContainerDb(admin_db, "Operational Database", "PostgreSQL 18", "Storage layer. Mutable state: transaction lifecycle, sync checkpoint, entity state, mempool collisions, config, audit logs, and the auth tables (users, magic_link_tokens, user_sessions).")
        ContainerDb(rawstore, "Data Lake", "Local Filesystem → S3/MinIO", "Storage layer. Write-once gzip JSON blobs (confirmed/ and mempool/ prefixes). Schema-on-read. Source of truth for raw Ogmios payloads.")
    }

    System_Ext(ogmios, "Ogmios v6", "WebSocket bridge to Cardano Node")
    System_Ext(mail, "SMTP Mail Relay", "Magic-link email delivery (mailpit in dev)")

    Rel(user, ui, "Browse and investigate; magic-link login", "HTTPS / WSS")
    Rel(ui, api, "Fetch data", "REST + WebSocket")
    Rel(connector, ogmios, "ChainSync + LocalTxMonitor + LocalStateQuery", "WebSocket :1337")
    Rel(connector, datalake, "Batch insert transactions", "Native :9000")
    Rel(connector, admin_db, "Write lifecycle state + sync checkpoint", "TCP :5432")
    Rel(connector, rawstore, "Write raw blobs async", "gzip JSON")
    Rel(analysis, datalake, "Read / write analysis results", "Native :9000")
    Rel(api, datalake, "Query transactions + analysis results", "Native :9000")
    Rel(api, admin_db, "Read/write lifecycle + config + entity state + auth/users", "TCP :5432")
    Rel(api, mail, "Send magic-link emails", "SMTP")
    Rel(api, clustering, "Reverse-proxy /api/clustering/* (Validators UI)", "HTTP")
    Rel(api, clusterdb, "Read contract_anomaly verdicts (merge into results)", "Native :9000")
    Rel(clustering, datalake, "Read watched-contract transactions", "HTTP :8123")
    Rel(clustering, clusterdb, "Write models + classifications + contract_anomaly", "HTTP :8123")
```

## Level 3: Component Diagram (FastAPI Application)

All four logical layers (Blockchain Connector, API Gateway, Analysis Engine, TMS Dashboard)
run as components within a single FastAPI async process (single-process architecture for Preprod).

```mermaid
C4Component
    title Component Diagram: FastAPI Application

    Container_Boundary(api, "FastAPI Application") {

        Component(lifespan, "Lifespan Manager", "Python asynccontextmanager", "Initializes DB connections, starts background tasks, handles graceful shutdown.")

        Component(ogmios_client, "Ogmios Client", "websockets", "Maintains three persistent WebSocket connections to Ogmios: chain (ChainSync), mempool (LocalTxMonitor), query (LocalStateQuery). Circuit breaker + exponential backoff on chain and mempool. UTxO query connection reconnects on error with graceful degradation.")

        Component(chain_sync, "ChainSync Task", "asyncio background task", "Subscribes to new blocks. On rollForward: applies cached UTxO input data (_apply_resolved_inputs), emits TX_CONFIRMED. On rollBackward: emits TX_ROLLED_BACK.")

        Component(mempool_monitor, "Mempool Monitor Task", "asyncio background task", "Polls mempool via LocalTxMonitor. For each new transaction: inserts TX_PENDING, resolves inputs via queryLedgerState/utxo, stores results in _pending_input_cache.")

        Component(parser, "Transaction Parser", "Python module", "Normalizes Ogmios v6 JSON into NormalizedTransaction. Captures block_index for MEV analysis.")

        Component(analysis_engine, "Analysis Engine", "asyncio background task", "Polls Analytics Warehouse for unscored transactions. 9-class Polimi detection engine assigns multi-class risk scores and labels.")

        Component(tx_api, "Transaction API", "FastAPI Router", "GET /api/transactions/, /api/transactions/{hash}, /api/transactions/address/{addr}, /api/transactions/stats/summary")

        Component(lifecycle_api, "Lifecycle API", "FastAPI Router", "GET /api/lifecycle/{txId}, /api/lifecycle?status=, /api/lifecycle/stats/summary")

        Component(analysis_api, "Analysis API", "FastAPI Router", "GET /api/analysis/results, /api/analysis/results/{hash}, /api/analysis/stats")

        Component(entity_api, "Entity API", "FastAPI Router", "GET/PUT /api/entities/{type}/{id}")

        Component(ws_router, "WebSocket Router", "FastAPI WebSocket", "WS /ws: broadcasts real-time lifecycle events to connected clients.")

        Component(ui_router, "UI Router", "FastAPI Router", "GET /: serves the TMS Dashboard (HTML5).")

        Component(auth_api, "Auth API", "FastAPI Router", "POST /api/auth/request-link, GET /api/auth/verify, POST /api/auth/logout, GET /api/auth/me. Magic-link login + session lifecycle.")

        Component(users_api, "Users API", "FastAPI Router", "GET/POST /api/users, DELETE /api/users/{id}, POST /api/users/{id}/resend-invite. Admin-gated user management.")

        Component(archive_api, "Archive API", "FastAPI Router", "GET/POST/DELETE /api/archive/*. False-positive curation and export.")

        Component(auth, "Auth Module", "FastAPI Security (app/auth)", "Two paths: TMS-API-Key header validation (constant-time, api_key.py) and magic-link sessions (tokens.py, sessions.py, email.py). deps.py exposes require_user / require_admin. Open API-key access requires empty API_KEYS + TMS_ALLOW_DEV_MODE=1, else startup aborts.")

        Component(rate_limiter, "Rate Limiter", "Middleware", "Per-key/IP sliding-window rate limiter. Configurable via RATE_LIMIT_REQUESTS / RATE_LIMIT_WINDOW_SECONDS.")

        Component(ch_adapter, "ClickHouse Adapter", "clickhouse-driver", "Analytics Warehouse. Schema management (clickhouse_schema.py), idempotent migrations, batch inserts, score read/write (clickhouse_scores.py), analytical queries.")

        Component(pg_adapter, "PostgreSQL Adapter", "asyncpg", "Operational Database. Connection pool, tx_lifecycle CRUD, sync_checkpoint, entity_state, audit logging, auth schema (users / magic_link_tokens / user_sessions), schema management.")

        Component(raw_store, "Raw Store Adapter", "gzip + ThreadPoolExecutor", "Data Lake. Async atomic writes of gzip JSON blobs to local filesystem (confirmed/ and mempool/ prefixes). Upgrade path: swap for boto3 S3 calls.")

        Component(config, "Config", "Pydantic Settings", "Loads .env, validates settings, provides typed access.")
    }

    System_Ext(ogmios, "Ogmios v6")
    System_Ext(mail, "SMTP Mail Relay")
    ContainerDb(datalake, "Analytics Warehouse (ClickHouse)")
    ContainerDb(admin_db, "Operational Database (PostgreSQL)")
    ContainerDb(rawstore, "Data Lake (Filesystem)")

    Rel(lifespan, ogmios_client, "Initializes")
    Rel(lifespan, chain_sync, "Starts")
    Rel(lifespan, mempool_monitor, "Starts")
    Rel(lifespan, analysis_engine, "Starts")
    Rel(lifespan, ch_adapter, "Initializes")
    Rel(lifespan, pg_adapter, "Initializes")

    Rel(ogmios_client, ogmios, "WebSocket JSON-RPC 2.0")

    Rel(chain_sync, ogmios_client, "findIntersection, nextBlock")
    Rel(chain_sync, parser, "Raw block data")
    Rel(chain_sync, ch_adapter, "Batch insert transactions")
    Rel(chain_sync, pg_adapter, "Upsert tx_lifecycle → CONFIRMED / ROLLED_BACK; save sync checkpoint")
    Rel(chain_sync, raw_store, "Write confirmed blobs async")
    Rel(chain_sync, ws_router, "Broadcast TX_CONFIRMED / TX_ROLLED_BACK")

    Rel(mempool_monitor, ogmios_client, "acquireMempool, nextTransaction, queryLedgerState/utxo")
    Rel(mempool_monitor, pg_adapter, "Insert TX_PENDING")
    Rel(mempool_monitor, raw_store, "Write mempool blobs async")
    Rel(mempool_monitor, ws_router, "Broadcast TX_PENDING")

    Rel(analysis_engine, ch_adapter, "Read unscored txs, write results")

    Rel(tx_api, auth, "Validate API key")
    Rel(tx_api, rate_limiter, "Check rate limit")
    Rel(tx_api, ch_adapter, "Query transactions")

    Rel(lifecycle_api, auth, "Validate API key")
    Rel(lifecycle_api, rate_limiter, "Check rate limit")
    Rel(lifecycle_api, pg_adapter, "Query tx_lifecycle")

    Rel(analysis_api, auth, "Validate API key")
    Rel(analysis_api, rate_limiter, "Check rate limit")
    Rel(analysis_api, ch_adapter, "Query analysis results")

    Rel(entity_api, auth, "Validate API key")
    Rel(entity_api, pg_adapter, "Get/set entity state")

    Rel(archive_api, auth, "require_user session")
    Rel(archive_api, ch_adapter, "Read/write archive state")

    Rel(auth_api, auth, "Mint/verify magic-link, issue session")
    Rel(auth_api, pg_adapter, "Read/write users + magic_link_tokens + user_sessions")
    Rel(auth_api, mail, "Send magic-link email", "SMTP")

    Rel(users_api, auth, "require_admin session")
    Rel(users_api, pg_adapter, "CRUD users")
    Rel(users_api, mail, "Send invite magic-link", "SMTP")

    Rel(ch_adapter, datalake, "Native protocol :9000")
    Rel(pg_adapter, admin_db, "asyncpg :5432")
    Rel(raw_store, rawstore, "atomic gzip write / os.replace()")
```

## Level 4: Key Data Flows

### Flow 1: Transaction Lifecycle (Ogmios → Storage Layers)

```mermaid
sequenceDiagram
    participant Node as Cardano Node
    participant Ogmios as Ogmios v6
    participant MM as Mempool Monitor
    participant CS as ChainSync Task
    participant Parser as Tx Parser
    participant PG as Operational Database (PostgreSQL)
    participant CH as Analytics Warehouse (ClickHouse)
    participant FS as Data Lake (Filesystem)
    participant WS as WebSocket Clients

    Note over MM,Ogmios: Mempool Monitoring (LocalTxMonitor + LocalStateQuery)
    MM->>Ogmios: acquireMempool
    Ogmios->>Node: Read mempool
    Node-->>Ogmios: Mempool snapshot
    Ogmios-->>MM: Acquired (slot)
    loop For each tx in snapshot
        MM->>Ogmios: nextTransaction (fields: all)
        Ogmios-->>MM: Transaction data
        MM->>Ogmios: queryLedgerState/utxo (input outputReferences)
        Note right of Ogmios: Inputs are guaranteed unspent in mempool
        Ogmios-->>MM: UTxO data (address, lovelace, assets)
        MM->>MM: cache → _pending_input_cache[tx_id]
        MM-)FS: write_mempool_async (gzip JSON → mempool/prefix)
        MM->>PG: INSERT tx_lifecycle (PENDING, first_seen_at)
        MM->>WS: Broadcast TX_PENDING
    end
    Ogmios-->>MM: null (snapshot exhausted)

    Note over CS,Ogmios: Block Monitoring (ChainSync)
    CS->>Ogmios: nextBlock
    Ogmios->>Node: ChainSync
    Node-->>Ogmios: New block
    Ogmios-->>CS: rollForward (block + transactions)
    CS->>Parser: Normalize block transactions (with block_index)
    Parser-->>CS: List[NormalizedTransaction]
    Note right of CS: _apply_resolved_inputs(): pop _pending_input_cache<br/>overwrite input address/amount, set total_input_value
    CS->>CH: Batch insert transactions + inputs + outputs (enriched)
    CS-)FS: write_confirmed_async (gzip JSON → confirmed/prefix)
    CS->>PG: UPDATE tx_lifecycle → CONFIRMED (compute latency_ms)
    CS->>WS: Broadcast TX_CONFIRMED
    CS->>PG: UPSERT sync_checkpoint (slot, block_id)

    Note over CS,Ogmios: Rollback Handling
    CS->>Ogmios: nextBlock
    Ogmios-->>CS: rollBackward (point)
    CS->>PG: UPDATE tx_lifecycle → ROLLED_BACK
    CS->>PG: UPSERT sync_checkpoint (rollback point)
    CS->>MM: Clear mempool dedup set
    CS->>WS: Broadcast TX_ROLLED_BACK
```

### Flow 2: Reconnection & Circuit Breaker

```mermaid
stateDiagram-v2
    [*] --> Connecting: Service starts

    Connecting --> Connected: WebSocket open
    Connecting --> Backoff: Connection failed

    Connected --> Syncing: findIntersection (tip or saved checkpoint)
    Syncing --> Streaming: Intersection found
    Streaming --> Streaming: nextBlock / acquireMempool
    Streaming --> Backoff: WebSocket closed / error

    Backoff --> Connecting: delay = min(1s * 2^n, 60s) + jitter
    Backoff --> CircuitOpen: 5 consecutive failures

    CircuitOpen --> CircuitHalfOpen: 2 min cooldown elapsed
    CircuitHalfOpen --> Connecting: Probe attempt
    CircuitHalfOpen --> CircuitOpen: Probe failed

    note right of Streaming
        On each confirmed block:
        persist sync checkpoint to PostgreSQL
    end note

    note right of Connecting
        On startup:
        read last checkpoint from PostgreSQL
        if exists → resume from saved point
        if not → start from current tip
    end note
```

### Flow 3: API Request (Authenticated Query)

```mermaid
sequenceDiagram
    participant Client as API Client
    participant RL as Rate Limiter
    participant Auth as Auth Middleware
    participant API as Lifecycle API
    participant PG as Admin DB (PostgreSQL)
    participant CH as Datalake (ClickHouse)

    Client->>API: GET /api/lifecycle/{txId}<br/>TMS-API-Key: secret-key
    API->>Auth: Validate API key
    alt dev mode (API_KEYS empty + TMS_ALLOW_DEV_MODE=1)
        Auth-->>API: Allow (dev mode)
    else Valid key
        Auth-->>API: Authorized (hmac.compare_digest)
    else Invalid/missing key
        Auth-->>Client: 403 Forbidden
    end
    API->>RL: Check rate limit for key
    alt Within limit
        RL-->>API: Allowed
    else Exceeded
        RL-->>Client: 429 Too Many Requests
    end
    API->>PG: SELECT * FROM tx_lifecycle WHERE tx_id = ?
    PG-->>API: Lifecycle state (status, timestamps, latency_ms)
    API->>CH: SELECT * FROM transactions WHERE tx_hash = ?
    CH-->>API: Full transaction details (inputs, outputs, fees, block_index)
    API-->>Client: 200 OK {lifecycle + transaction data}
```

## Deployment View

Ogmios runs alongside the Cardano node, outside the TMS Docker Compose network.
The TMS connects to it via `OGMIOS_WS_URL` (default: `ws://localhost:1337`).

```mermaid
C4Deployment
    title Deployment Diagram: Production

    Deployment_Node(server, "Server", "Linux VM / Bare Metal") {
        Deployment_Node(docker, "Docker Compose: TMS") {
            Deployment_Node(app_container, "tms-app") {
                Container(app, "FastAPI App", "Python", "Uvicorn ASGI server: all four logical layers in one process")
                ContainerDb(fs_inst, "Data Lake", "Docker named volume (raw_store_data)", "Write-once gzip JSON blobs. confirmed/ and mempool/ prefixes. Upgrade path: MinIO → S3/R2/B2.")
            }
            Deployment_Node(pg_container, "tms-postgres") {
                ContainerDb(pg_inst, "Operational Database", "PostgreSQL 18", "tx_lifecycle, sync_checkpoint, entity_state, mempool_collisions, audit_logs, config, users, magic_link_tokens, user_sessions")
            }
            Deployment_Node(ch_container, "tms-clickhouse") {
                ContainerDb(ch_inst, "Analytics Warehouse", "ClickHouse 26.1 MergeTree", "transactions, transaction_inputs, transaction_outputs, address_transactions, tx_class_scores, baselines")
            }
            Deployment_Node(mail_container, "tms-mailpit") {
                Container(mail_inst, "Mailpit", "Go", "Dev SMTP sink + webmail (:1025 SMTP, :8025 UI). Swap for a real relay in production.")
            }
        }
        Deployment_Node(node_infra, "Cardano Node Infrastructure") {
            Container(ogmios_inst, "Ogmios v6.14.0", "Haskell", "WebSocket bridge on :1337")
            Container(node, "Cardano Node 11.0.1", "cardano-node", "Full node (mainnet, preprod, or preview); 11.0.1 required for van Rossem PV11")
        }
    }

    Rel(ogmios_inst, node, "Node-to-Client IPC", "Unix socket")
    Rel(app, ogmios_inst, "JSON-RPC 2.0", "WebSocket :1337")
    Rel(app, pg_inst, "asyncpg", "TCP :5432")
    Rel(app, ch_inst, "clickhouse-driver", "Native :9000")
    Rel(app, mail_inst, "Magic-link emails", "SMTP :1025")
```
