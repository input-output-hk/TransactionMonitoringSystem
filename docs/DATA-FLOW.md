# Data Flow: Cardano Transaction Monitoring System

Three diagrams covering the system's runtime behaviour:

1. [Chain Sync: block ingestion end-to-end](#1-chain-sync-path)
2. [Storage Map: what lands where and why](#2-storage-map)
3. [Transaction Lifecycle: state transitions](#3-transaction-lifecycle-states)

## 1. Chain Sync Path

How a Cardano block travels from the node to every storage layer and connected client.

Three persistent WebSocket connections to Ogmios run independently at all times: chain sync (`nextBlock` loop), mempool (`nextTransaction` loop), and a query connection (`queryLedgerState/utxo`).
This diagram covers the **chain sync** connection.
The mempool connection writes to `tx_lifecycle` (PENDING), the raw store (`mempool/` prefix), and resolves input UTxOs via the query connection, storing results in `_pending_input_cache` for use at confirmation time. Those paths are not shown in the sequence below; see the Storage Map in section 2.

```mermaid
sequenceDiagram
    autonumber

    participant Ogmios as Ogmios v6
    participant CS     as Chain Sync
    participant Parser as Parser
    participant CH     as Analytics Warehouse (ClickHouse)
    participant FS     as Data Lake (Filesystem)
    participant PG     as Operational Database (PostgreSQL)
    participant WS     as WebSocket Clients

    Note over CS,PG: Application startup: resume from last known position
    CS->>PG: get_sync_point(network)
    PG-->>CS: {slot, block_id} or None

    CS->>Ogmios: findIntersection({slot, id})
    Note right of Ogmios: First run: intersect at chain tip.<br/>Restart: intersect at saved slot.
    Ogmios-->>CS: intersection confirmed

    loop Every Cardano block (~20 s on Preprod)
        CS->>Ogmios: nextBlock
        Ogmios-->>CS: RollForward {block_id, slot, height, transactions[]}

        loop Each transaction in block
            CS->>Parser: parse_ogmios_transaction(tx_data, slot, block_hash, height, index)
            Parser-->>CS: NormalizedTransaction
            Note right of CS: _apply_resolved_inputs(): pop _pending_input_cache[tx_hash]<br/>if found: overwrite input address/amount, set total_input_value
        end

        Note over CS,CH: Async: runs on thread pool, does not block PostgreSQL writes
        CS-)CH: insert_transactions_batch_async(normalized_txs)
        activate CH
        CH->>CH: INSERT transactions        (1 row per tx)
        CH->>CH: INSERT transaction_inputs  (1 row per input)
        CH->>CH: INSERT transaction_outputs (1 row per output)
        CH-->>CH: Materialized view fires → INSERT address_transactions
        deactivate CH

        CS-)FS: write_confirmed_async × N (gzip JSON to {network}/{YYYYMMDD}/{shard}/{tx_hash}.json.gz)

        CS->>PG: batch_upsert_lifecycle_confirmed(records)
        Note right of PG: ON CONFLICT (tx_id) DO UPDATE<br/>status = CONFIRMED<br/>confirmed_at, block_hash, slot, latency_ms
        PG-->>CS: OK

        CS->>WS: broadcast TX_CONFIRMED × N

        CS->>PG: save_sync_point(network, slot, block_id)
        PG-->>CS: OK
    end

    alt Chain reorganisation (RollBackward)
        CS->>Ogmios: nextBlock
        Ogmios-->>CS: RollBackward {rollback_slot, rollback_id}

        CS->>PG: mark_lifecycle_rolled_back(rollback_slot, network)
        Note right of PG: UPDATE tx_lifecycle<br/>SET status = ROLLED_BACK<br/>WHERE slot > rollback_slot

        CS->>PG: save_sync_point(network, rollback_slot, rollback_id)
        Note over CS: Mempool dedup set cleared.<br/>Rolled-back txs may re-enter mempool.
        CS->>WS: broadcast TX_ROLLED_BACK
    end
```

**Key design points:**

- ClickHouse and raw store writes are shown as `-)` (async arrows) because they run on thread pools; the event loop stays responsive and PostgreSQL writes from the mempool monitor can proceed concurrently. However, the chain sync coroutine itself **awaits** both before saving the checkpoint.
- The sync checkpoint is saved **last**, after all storage writes and the WebSocket broadcast. If the process crashes before the checkpoint save, the block is reprocessed on restart. ClickHouse `MergeTree` deduplication and raw store write-once logic (atomic temp-file + rename) handle the duplicate insert gracefully.
- The raw store uses an atomic write pattern (`gzip → .tmp`, then `os.replace()`). A crash mid-write leaves only the `.tmp` file; the final path does not exist, so the write is retried on replay.
- The materialized view `address_transactions_mv` fires automatically on every `INSERT INTO transactions`; no application code required.
- **UTxO input resolution**: when a PENDING tx is observed by the mempool monitor, its inputs are guaranteed unspent (the node validates this before admitting the tx). The system immediately calls `queryLedgerState/utxo` on a third WebSocket connection and caches address + lovelace data per input. When ChainSync confirms the block (~20 s later), those inputs are already spent and no longer queryable; the cached data is applied instead, populating `total_input_value` and input addresses in ClickHouse. For txs confirmed without prior mempool observation, `total_input_value` remains `NULL`.

## 2. Storage Map

What data enters the system, what transforms it, and where it lands.
The vertical axis represents the journey from raw chain event to queryable storage.

```mermaid
flowchart TD
    OG(["Ogmios v6: WebSocket"])

    subgraph ingest["Ingestion: ogmios_client.py"]
        CS["Chain Sync: nextBlock loop"]
        MM["Mempool Monitor: nextTransaction loop"]
        QC["Query Connection: queryLedgerState/utxo"]
        CACHE[("_pending_input_cache: in-memory")]
    end

    subgraph parse["Parser: ogmios_parser.py"]
        P["parse_ogmios_transaction()"]
    end

    subgraph ch["Analytics Warehouse (ClickHouse)"]
        T[("transactions")]
        TI[("transaction_inputs")]
        TO[("transaction_outputs")]
        AT[("address_transactions: auto via MV")]
        AR[("tx_class_scores")]
        BL[("baselines: per-script/policy percentiles")]
    end

    subgraph pg["Operational Database (PostgreSQL)"]
        LC[("tx_lifecycle: current status")]
        SC[("sync_checkpoint: last slot")]
        ES[("entity_state: user annotations")]
    end

    subgraph fs["Data Lake (Filesystem)"]
        RC[("confirmed/{network}/{YYYYMMDD}/{shard}")]
        RM[("mempool/{network}/{YYYYMMDD}/{shard}")]
    end

    subgraph ae["Analysis Engine: engine.py (background)"]
        AE["_score_transaction()"]
    end

    OG -->|"RollForward"| CS
    OG -->|"nextTransaction"| MM
    OG -->|"queryLedgerState/utxo"| QC

    MM -->|"resolve inputs"| QC
    QC -->|"cache UTxO data"| CACHE
    CACHE -->|"pop at confirmation"| CS

    CS --> P
    P -->|"INSERT async"| T
    P -->|"INSERT async"| TI
    P -->|"INSERT async"| TO
    T -.->|"materialized view"| AT

    CS -->|"UPSERT CONFIRMED"| LC
    CS -->|"UPSERT checkpoint"| SC
    CS -.->|"write_confirmed async"| RC

    MM -->|"INSERT PENDING"| LC
    MM -.->|"write_mempool async"| RM

    T  -->|"SELECT unscored"| AE
    TI -.->|"JOIN inputs"| AE
    TO -.->|"JOIN outputs"| AE
    AT -.->|"address activity"| AE
    AE -->|"INSERT scores"| AR

    style ch     fill:#ffe0b2,stroke:#e65100,color:#000000,stroke-width:2px
    style pg     fill:#bbdefb,stroke:#1565c0,color:#000000,stroke-width:2px
    style fs     fill:#dcedc8,stroke:#558b2f,color:#000000,stroke-width:2px
    style CACHE  fill:#fff9c4,stroke:#f9a825,color:#000000,stroke-width:2px
    style ingest fill:#e1bee7,stroke:#6a1b9a,color:#000000,stroke-width:2px
    style parse  fill:#c8e6c9,stroke:#2e7d32,color:#000000,stroke-width:2px
    style ae     fill:#f8bbd0,stroke:#880e4f,color:#000000,stroke-width:2px
```

**Three storage layers:**

| | Analytics Warehouse (ClickHouse) | Operational Database (PostgreSQL) | Data Lake (Filesystem) |
|---|---|---|---|
| **Role** | Structured facts (derived, queryable) | Mutable state (current lifecycle status) | Raw blobs: full Ogmios payloads, source of truth |
| **Mutation** | Append-only; no row-level UPDATE | Row-level UPDATE/DELETE | Write-once files |
| **Consistency** | Eventually consistent (`FINAL` for latest) | Strongly consistent | N/A; keyed by (prefix, network, date, tx_hash) |
| **Query strength** | Scan millions of rows · GROUP BY · aggregations | Single-row lookup by primary key · transactional upsert | Key lookup by (prefix, network, date, tx_hash) |
| **Data growth** | Unbounded time-series | Bounded; one row per tx | Unbounded; one file per tx |
| **Production upgrade** | - | - | MinIO → S3/R2/B2 |

The Data Lake (filesystem) is the **source of truth** for raw payloads; the Analytics Warehouse (ClickHouse) is derived from it and can be reconstructed by replaying raw files. The `tx_lifecycle` table (PostgreSQL) is the bridge for current state: it owns the **authoritative current status** of every transaction (PENDING / CONFIRMED / ROLLED_BACK / DROPPED), while ClickHouse owns the **full historical record** for analytics, and the filesystem holds the **full raw payloads** for replay and debugging.

## 3. Transaction Lifecycle States

How a transaction moves through the system from first observation to final state.

```mermaid
stateDiagram-v2
    direction LR

    [*]          --> PENDING      : Seen in mempool
    PENDING      --> CONFIRMED    : Block confirmed
    PENDING      --> DROPPED      : TTL expired
    CONFIRMED    --> ROLLED_BACK  : Chain reorg
    ROLLED_BACK  --> PENDING      : Tx resubmitted
    DROPPED      --> CONFIRMED    : Late confirmation
    CONFIRMED    --> [*]
    DROPPED      --> [*]
    ROLLED_BACK  --> [*]
```

**State definitions:**

| State | Meaning | Where stored |
|---|---|---|
| **PENDING** | Seen in mempool, not yet on-chain | PostgreSQL `tx_lifecycle` + filesystem `mempool/` (best-effort; the monitor may miss a tx if it was not observed before confirmation) |
| **CONFIRMED** | Included in a confirmed block | PostgreSQL `tx_lifecycle` + ClickHouse `transactions` + filesystem `confirmed/` (authoritative) |
| **ROLLED_BACK** | Was confirmed, block later reorganised away | PostgreSQL `tx_lifecycle` only; ClickHouse row and filesystem `confirmed/` file remain |
| **DROPPED** | Was PENDING, TTL expired without confirmation | PostgreSQL `tx_lifecycle` only; filesystem `mempool/` file remains if the tx was observed by the monitor |

**Notable transitions:**

- **PENDING → DROPPED → CONFIRMED**: A transaction can be dropped from monitoring (TTL expired) but still confirm on-chain later. `batch_upsert_lifecycle_confirmed` uses `ON CONFLICT ... DO UPDATE`, so the status is always corrected to CONFIRMED when a block arrives containing the tx.
- **CONFIRMED → ROLLED_BACK → PENDING**: After a chain reorg, the mempool dedup set is cleared. If the user resubmits the rolled-back transaction, it re-enters as PENDING. In Cardano, reorgs are rare and shallow (1–2 blocks).
- **ClickHouse and filesystem are write-once**: A ROLLED_BACK transaction leaves a row in `transactions` and a file in `confirmed/`. The lifecycle API (PostgreSQL) is the source of truth for current status.
