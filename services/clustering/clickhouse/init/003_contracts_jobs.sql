-- Contract registry + pipeline-job tracking for the UI-driven onboarding flow.
-- Auto-loaded on first container start (alongside 001/002); also applied by hand
-- to an already-initialised ClickHouse volume.
--
-- `present` is the existence flag (named `present` rather than `exists` to avoid
-- clashing with ClickHouse's EXISTS keyword); the API exposes it as `exists`.

CREATE DATABASE IF NOT EXISTS tms;

-- One row per onboarded contract: identity metadata + processing status.
-- Refreshed in place by service.process_contract; latest row wins (updated_at).
CREATE TABLE IF NOT EXISTS tms.contracts
(
    target              String,
    target_type         Enum8('address' = 1, 'policy' = 2),
    label               String DEFAULT '',
    present             UInt8,                 -- 1 if the address/policy was found
    is_script           UInt8,
    script_type         String DEFAULT '',     -- plutusV1/V2/V3, timelock/native, ''
    balance_lovelace    Int128,                -- 0 for policy targets
    asset_count         UInt32,
    sample_tokens       String DEFAULT '[]',   -- JSON: [{unit, policy_id, name}]
    tx_count            UInt32,
    status              Enum8('pending' = 1, 'processing' = 2, 'done' = 3, 'failed' = 4),
    requested_max_txs   UInt32,                 -- backfill DOWNLOAD depth (0 = unbounded)
    target_txs          UInt32 DEFAULT 0,       -- "latest N to cluster on" read window (0 = ceiling); see 010
    updated_at          DateTime64(6) DEFAULT now64(6)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (target);

-- One row per onboarding/refresh job, updated as the pipeline advances so the
-- UI can poll progress. Latest row wins (updated_at).
CREATE TABLE IF NOT EXISTS tms.jobs
(
    job_id          String,
    target          String,
    target_type     Enum8('address' = 1, 'policy' = 2),
    max_txs         UInt32,                     -- 0 = unbounded
    reprocess       UInt8,
    status          Enum8('queued' = 1, 'checking' = 2, 'downloading' = 3,
                          'clustering' = 4, 'scoring' = 5, 'done' = 6, 'failed' = 7),
    stage_detail    String DEFAULT '',
    txs_done        UInt32,
    error           String DEFAULT '',
    created_at      DateTime64(6) DEFAULT now64(6),
    updated_at      DateTime64(6) DEFAULT now64(6)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (job_id);
