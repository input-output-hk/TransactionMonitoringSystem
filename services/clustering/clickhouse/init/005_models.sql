-- Persisted cluster models (fit artifacts) + per-transaction online
-- classifications. Together these enable incremental scoring of NEW transactions
-- against a frozen model — the online half of the fit/score split (see
-- docs/online-classification-design.md) — without re-running batch DBSCAN.
-- Auto-loaded on a fresh volume (after 003); apply by hand to an existing one.

CREATE DATABASE IF NOT EXISTS tms;

-- The current fitted model per (target, feature_set). `blob` is the serialized
-- artifact (RobustScaler params, per-cluster centroids/radii/verdict snapshot,
-- fitted IsolationForest/LOF, per-method vote thresholds). The ORDER BY is
-- (target, feature_set) — NOT including model_id — so a re-fit collapses the
-- previous model (newest created_at wins) instead of accumulating ~7 MB blobs
-- forever. Only the latest model is ever read.
CREATE TABLE IF NOT EXISTS tms.cluster_models
(
    model_id        String,
    target          String,
    feature_set     Enum8('shape' = 1, 'graph' = 2, 'combined' = 3),
    run_id          String,            -- source cluster run the model was fit from
    schema_version  UInt16,
    n_clusters      UInt16,
    n_train         UInt32,            -- training-set size
    eps             Float64,
    min_samples     UInt32,
    blob            String,            -- base64(joblib) serialized artifact
    created_at      DateTime64(6) DEFAULT now64(6)
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (target, feature_set);

-- One row per transaction classified online against a model. Higher score = more
-- anomalous; cluster_id = -1 means unassigned (the online analogue of DBSCAN
-- noise). Keyed by (target, feature_set, tx_hash) so a re-score replaces.
CREATE TABLE IF NOT EXISTS tms.tx_classifications
(
    target          String,
    tx_hash         FixedString(64),
    feature_set     Enum8('shape' = 1, 'graph' = 2, 'combined' = 3),
    model_id        String,
    cluster_id      Int32,
    iso_score       Float64,
    lof_score       Float64,
    votes           UInt8,
    consensus       Float64,
    verdict         Enum8('malicious' = 1, 'benign' = 2, 'anomaly' = 3, 'normal' = 4),
    scored_at       DateTime64(6) DEFAULT now64(6)
)
ENGINE = ReplacingMergeTree(scored_at)
ORDER BY (target, feature_set, tx_hash);

-- Distinguish onboarding/refresh jobs from incremental classify jobs so the
-- worker can dispatch to the right pipeline. Existing rows default to 'onboard'.
ALTER TABLE tms.jobs ADD COLUMN IF NOT EXISTS kind String DEFAULT 'onboard';
