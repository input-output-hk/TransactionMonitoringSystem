-- Schema for the Cardano contract transaction clustering tool.
-- Auto-loaded by the ClickHouse entrypoint on first container start.
-- All objects are fully qualified with the `tms` database so the script is
-- independent of the connecting client's default database.

CREATE DATABASE IF NOT EXISTS tms;

-- One row per transaction that interacts with a target (address or policy).
CREATE TABLE IF NOT EXISTS tms.transactions
(
    target                      String,
    target_type                 Enum8('address' = 1, 'policy' = 2),
    tx_hash                     FixedString(64),
    block_height                UInt64,
    block_time                  DateTime,
    slot                        UInt64,
    fees                        UInt64,            -- lovelace
    deposit                     Int64,             -- lovelace (can be negative on refund)
    size                        UInt32,            -- bytes
    valid_contract              UInt8,
    input_count                 UInt32,
    output_count                UInt32,
    total_input_lovelace        UInt64,
    total_output_lovelace       UInt64,
    distinct_input_addresses    UInt32,
    distinct_output_addresses   UInt32,
    distinct_assets             UInt32,
    redeemer_count              UInt32,
    ingested_at                 DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(ingested_at)
ORDER BY (target, tx_hash);

-- One row per UTXO (input or output) of a transaction. Drives address-graph features.
CREATE TABLE IF NOT EXISTS tms.tx_utxos
(
    target      String,
    tx_hash     FixedString(64),
    role        Enum8('input' = 1, 'output' = 2),
    idx         UInt32,
    address     String,
    lovelace    UInt64
)
ENGINE = ReplacingMergeTree
ORDER BY (target, tx_hash, role, idx, address);

-- One row per native asset moved in a UTXO. Drives asset features.
CREATE TABLE IF NOT EXISTS tms.tx_utxo_assets
(
    target      String,
    tx_hash     FixedString(64),
    role        Enum8('input' = 1, 'output' = 2),
    idx         UInt32,
    unit        String,            -- policy_id ++ hex(asset_name)
    quantity    Int128
)
ENGINE = ReplacingMergeTree
ORDER BY (target, tx_hash, role, idx, unit);

-- Resume cursor for incremental / rate-limited ingestion.
CREATE TABLE IF NOT EXISTS tms.ingest_cursor
(
    target          String,
    target_type     Enum8('address' = 1, 'policy' = 2),
    last_page       UInt32,
    last_tx_hash    String,
    txs_seen        UInt64,
    done            UInt8,
    updated_at      DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (target);

-- Metadata for each clustering run.
CREATE TABLE IF NOT EXISTS tms.cluster_runs
(
    run_id          String,
    target          String,
    feature_set     Enum8('shape' = 1, 'graph' = 2, 'combined' = 3),
    eps             Float64,
    min_samples     UInt32,
    metric          String,
    n_points        UInt32,
    n_clusters      UInt32,
    n_noise         UInt32,
    silhouette      Float64,            -- NaN when undefined (< 2 clusters)
    notes           String DEFAULT '',
    -- 'system' = auto-tuned by process_contract (the canonical run that feeds the
    -- online model); 'custom' = a user-supplied alternative via /api/cluster.
    origin          Enum8('system' = 1, 'custom' = 2) DEFAULT 'custom',
    created_at      DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (run_id);

-- Per-transaction cluster assignment for a run. cluster_id = -1 means noise.
CREATE TABLE IF NOT EXISTS tms.cluster_labels
(
    run_id      String,
    tx_hash     FixedString(64),
    cluster_id  Int32
)
ENGINE = ReplacingMergeTree
ORDER BY (run_id, cluster_id, tx_hash);
