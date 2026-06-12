# Data Flow: Plain-English Explanation

Companion to [DATA-FLOW.md](DATA-FLOW.md). That document contains the canonical diagrams; this one explains what they show and why the system is built that way.

## Overview

The system watches a Cardano network in real time by maintaining three permanent WebSocket connections to an Ogmios node. One connection follows the chain (confirmed blocks), one watches the mempool (unconfirmed transactions), and a third issues `queryLedgerState/utxo` calls to resolve input UTxO data for pending transactions. Everything the system learns is written to three independent storage layers: a relational database for mutable state, a columnar warehouse for analytics, and a flat-file data lake for raw payloads.

## 1. Chain Sync: How a Block Travels Through the System

### Startup and resumption

Before asking for new blocks, the chain sync component checks PostgreSQL for the last saved sync point (a `{slot, block_id}` pair). If one exists, it tells Ogmios to resume from that slot; if not (first run), it starts from the chain tip. This ensures a restart after a crash does not silently skip blocks.

### Block ingestion loop

Every ~20 seconds on Cardano's pre-production network, Ogmios delivers a `RollForward` message containing a block: its ID, slot number, height, and the full list of transactions inside it.

The chain sync component feeds each transaction to the **parser**, which normalises the raw Ogmios JSON into a consistent internal structure (`NormalizedTransaction`). Parsing is synchronous; nothing else happens until every transaction in the block has been parsed.

### Writing to storage: three sequential writes

Once parsing and input enrichment are complete, three writes happen in order:

**1. ClickHouse (async, on a thread pool)**
The batch of enriched, normalised transactions is inserted into three tables: `transactions` (one row per tx), `transaction_inputs` (one row per input UTXO spent), and `transaction_outputs` (one row per output UTXO created). A materialized view fires automatically on every `transactions` insert, populating `address_transactions`, a denormalised index that lets you query all transactions for a given address without touching the application layer.

**2. Filesystem / Data Lake (async, gathered in parallel per-tx)**
Each transaction is serialised to gzip-compressed JSON and written to `confirmed/{network}/{YYYYMMDD}/{shard}/{tx_hash}.json.gz`. The write uses an atomic pattern: the file is written to a `.tmp` path first, then renamed with `os.replace()`. A crash mid-write leaves only the `.tmp` file; the final path never exists in a partial state, so the write is safe to retry on replay.

**3. PostgreSQL lifecycle upsert (awaited, in the event loop)**
The lifecycle table is updated with `ON CONFLICT ... DO UPDATE`, setting each transaction's status to `CONFIRMED` and recording the block hash, slot, and confirmation latency.

### Broadcasting to connected clients

After all three writes complete, a `TX_CONFIRMED` event is broadcast over WebSocket to any connected clients. The broadcast happens before the checkpoint save so that the event cannot be lost if a crash occurs between broadcast and checkpoint.

### Checkpoint save

After the broadcast, the chain sync component saves the new `{slot, block_id}` as the sync checkpoint. If the process crashes between the writes and this save, the block is reprocessed on the next restart. Both ClickHouse (MergeTree deduplication) and the filesystem (write-once atomic rename) handle the duplicate gracefully.

### Chain reorganisations

If Ogmios sends a `RollBackward` message, the chain sync component:

1. Updates PostgreSQL to mark all `tx_lifecycle` rows with a slot greater than the rollback slot as `ROLLED_BACK`.
2. Saves the new (earlier) sync point.
3. Clears the mempool deduplication set, because rolled-back transactions may re-enter the mempool.
4. Broadcasts `TX_ROLLED_BACK` to clients.

The ClickHouse rows and filesystem files for rolled-back transactions are **not deleted**; they remain as a historical record. The lifecycle table in PostgreSQL is the single source of truth for a transaction's current status.

## 2. Storage Map: What Goes Where and Why

Three storage layers serve distinct roles:

### PostgreSQL: Operational Database (mutable state)

PostgreSQL holds the current status of every transaction ever seen, plus the sync checkpoint and any user-managed entity annotations. Its strength is row-level transactional updates: a transaction's status can flip from `PENDING` to `CONFIRMED` to `ROLLED_BACK` with `ON CONFLICT` upserts. The dataset size is bounded (one row per transaction, no history), which keeps single-row lookups fast.

### ClickHouse: Analytics Warehouse (structured facts)

ClickHouse holds the full structured history of every confirmed transaction, decomposed into three related tables plus the materialized address index. It is append-only with no row-level updates. Its strength is scanning millions of rows quickly: aggregations, GROUP BY queries, range scans over slots or time. Because it is derived from the raw data, it can be reconstructed from the filesystem if needed. The `FINAL` keyword is used when querying the latest version of a deduplicated row.

### Filesystem: Data Lake (raw blobs)

The filesystem holds the complete raw Ogmios payload for every transaction, both mempool observations (`mempool/` prefix) and confirmed transactions (`confirmed/` prefix). It is the system's source of truth: if ClickHouse needs to be rebuilt, or if a bug is suspected in the parser, the raw files can be replayed. Files are write-once and keyed by `(prefix, network, date, tx_hash)`. In production this layer can be backed by object storage (S3, R2, B2) without changing the interface.

### Analysis Engine

A background process queries ClickHouse for unscored transactions and runs a multi-class detection pipeline. The query admits a transaction only when its inputs are visible: either `input_count = 0` (treasury or collateral-only edge cases that need no input enrichment) or at least one row exists in `transaction_inputs` for that tx. ClickHouse `INSERT` statements are atomic per-statement, so "any row exists" is a sufficient witness that all input rows for the tx are visible. This guarantees that downstream scorers receive complete UTxO context regardless of where in the ingestion pipeline the analysis poll lands.

Each batch then goes through four enrichment phases before scoring:

1. **Input address resolution**: resolves input addresses from `transaction_inputs` table and patches `raw_data` in-place so scorers have complete UTxO context.
2. **Collision enrichment**: queries PostgreSQL `mempool_collisions` for transactions involved in UTxO input collisions or displacements (feeds the Front-Running scorer).
3. **Cycle enrichment**: runs bounded BFS in the transfer graph to detect value cycles returning to origin within 6 hops (feeds the Circular scorer).
4. **Sandwich enrichment**: detects a wallet attacker's two legs bracketing a victim's tx (in `(slot, block_index)` order) at the same script address within a 5-slot window, with a net-ADA profit floor (feeds the Sandwich scorer).

After enrichment, each transaction is scored by 9 independent attack-class scorers (Token Dust, Large Value, Large Datum, Multiple Satisfaction, Front-Running, Sandwich, Circular Transfers, Fake Token, Phishing). Each scorer has a gate condition (cheap check to skip inapplicable transactions) and a score function (weighted sub-score composition producing a 0-100 risk score). Sub-scores use percentile-based normalisation against per-script or per-policy baselines, falling back to global baselines or fixed anchors.

The output is a 9-element score vector per transaction, written to `tx_class_scores` in ClickHouse along with the max score, max class, risk band (Informational/Moderate/High/Critical), sub-score breakdowns, per-class evidence, and a cross-class corroboration count (how many distinct classes scored above the corroboration threshold). The corroboration count is a triage flag only: it does not affect the risk band.

## 3. Transaction Lifecycle: State Transitions

### States

| State | Meaning |
|---|---|
| `PENDING` | Seen in the mempool, not yet confirmed on-chain |
| `CONFIRMED` | Included in a block that the chain has not reorganised away |
| `ROLLED_BACK` | Was confirmed, but the block it was in was later reorganised away |
| `DROPPED` | Was pending, but the TTL expired without on-chain confirmation |

All state is stored in the `tx_lifecycle` table in PostgreSQL. ClickHouse and the filesystem do not reflect status changes after the initial write.

### Normal path

A transaction first appears in the mempool. The mempool monitor immediately resolves the transaction's inputs via `queryLedgerState/utxo` on the third WebSocket connection, before any write. Because a transaction can only enter the mempool if all its inputs are currently unspent, this query is guaranteed to return data at observation time. The resolved address and lovelace amount for each input are stored in an in-memory cache keyed by `(input_tx_hash, input_index)`. After the UTxO query, the monitor writes the raw payload to `mempool/` on the filesystem, inserts a `PENDING` row to `tx_lifecycle` in PostgreSQL, and broadcasts `TX_PENDING` to connected clients, in that order.

When the chain sync component sees the transaction in a confirmed block (~20 s later), it pops the cache entry and applies the resolved input data to the `NormalizedTransaction` before inserting into ClickHouse, populating `total_input_value` and the per-input `address` and `amount` columns. By that point the inputs are spent and can no longer be queried from the ledger, so the cache is the only source for this data.

For transactions confirmed without a prior mempool observation, `total_input_value` remains `NULL` and input addresses remain empty; this is the unresolved path and is expected behaviour.

### Edge cases worth noting

**PENDING to DROPPED to CONFIRMED**
The TTL expiry sweep marks a transaction `DROPPED` if it has not confirmed within the expected window. However, `batch_upsert_lifecycle_confirmed` uses `ON CONFLICT ... DO UPDATE` with no status guard, so if the transaction does eventually appear in a block, its status is unconditionally corrected to `CONFIRMED`. A transaction cannot get stuck in `DROPPED` if it actually confirmed.

**CONFIRMED to ROLLED_BACK to PENDING**
After a chain reorg, rolled-back transactions may be resubmitted to the mempool. The deduplication set is cleared on rollback, so the mempool monitor can observe the transaction again and write a fresh `PENDING` row.

**Mempool monitoring is best-effort**
The mempool monitor is a separate WebSocket connection and may not observe a transaction if it was submitted and confirmed very quickly. In that case the `tx_lifecycle` row is written directly as `CONFIRMED` by the chain sync path, with no prior `PENDING` row. The filesystem `mempool/` file will not exist for such transactions.

## Design Philosophy

**Three independent WebSocket connections.**
Chain sync, mempool monitoring, and UTxO queries each hold their own connection. The `queryLedgerState` protocol cannot be interleaved on the same connection as `LocalTxMonitor` or `ChainSync` (the sequential send-receive pattern would break protocol state), so a dedicated third connection is required. A failure in the query connection resets only itself and returns empty results; it never blocks block ingestion or mempool monitoring.

**The checkpoint is saved last.**
By saving the sync checkpoint only after all writes complete, the system guarantees at-least-once delivery to every storage layer. Duplicate handling (ClickHouse deduplication, atomic filesystem writes) makes this safe.

**PostgreSQL is never used for analytics.**
All aggregate queries go to ClickHouse. PostgreSQL rows are small, bounded, and optimised purely for transactional correctness.

**The filesystem is the escape hatch.**
Having the full raw payload on disk means no analytics decision is irreversible. The parser, the schema, and the scoring logic can all be changed and replayed from the raw files without re-syncing from the Cardano node.

**UTxO data is captured at the only window where it is available.**
Once a transaction is confirmed, its inputs are spent and `queryLedgerState/utxo` will not return them. The mempool observation window is the only opportunity to resolve input addresses and amounts without maintaining a separate full UTxO index. The in-memory cache bridges the ~20-second gap between mempool observation and block confirmation.
