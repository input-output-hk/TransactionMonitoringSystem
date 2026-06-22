-- Anomaly-detection results: ensemble per-transaction outlier scores.
-- Auto-loaded on first container start (alongside 001_schema.sql).

CREATE DATABASE IF NOT EXISTS tms;

-- Metadata for each anomaly-detection run.
CREATE TABLE IF NOT EXISTS tms.anomaly_runs
(
    run_id          String,
    target          String,
    feature_set     Enum8('shape' = 1, 'graph' = 2, 'combined' = 3),
    methods         String,            -- comma-separated detectors used
    n_points        UInt32,
    n_flagged       UInt32,            -- transactions with >= 2 method votes
    eps             Float64,           -- DBSCAN params used for the noise signal
    min_samples     UInt32,
    top_quantile    Float64,           -- per-method "flagged" threshold
    origin          Enum8('system' = 1, 'custom' = 2) DEFAULT 'custom',
    created_at      DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (run_id);

-- Per-transaction anomaly scores for a run. Higher score = more anomalous.
-- iso_score is NaN when Isolation Forest is not applicable (precomputed metric).
CREATE TABLE IF NOT EXISTS tms.anomaly_scores
(
    run_id          String,
    tx_hash         FixedString(64),
    iso_score       Float64,           -- Isolation Forest (higher = more anomalous)
    lof_score       Float64,           -- Local Outlier Factor (higher = more anomalous)
    dbscan_noise    UInt8,             -- 1 if DBSCAN labelled it noise
    consensus       Float64,           -- [0,1] mean of normalized signals
    votes           UInt8,             -- 0..3 detectors flagging it
    score_rank      UInt32             -- 1 = most anomalous
)
ENGINE = ReplacingMergeTree
ORDER BY (run_id, score_rank, tx_hash);
