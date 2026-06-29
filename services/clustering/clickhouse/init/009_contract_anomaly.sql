-- Contract-anomaly projection table: the per-(watched-contract, transaction)
-- verdicts the clustering sidecar publishes for the host TMS to read at API
-- time as the synthetic `contract_anomaly` attack class.
--
-- Stores the RAW engine outputs (verdict / consensus / votes / detector
-- scores) and deliberately NOT a host-scale 0-100 score: the host computes the
-- score from these via its `contract_anomaly` projection config, so the mapping
-- has a single source of truth. Keyed by (network, tx_hash, target) because one
-- transaction can be touched by several watched contracts; the host read-merge
-- collapses to the highest-severity verdict.
--
-- The `tms` database token is rewritten to the configured database by
-- `python -m app.cli migrate` (the integrated sidecar uses `tms_clustering` on
-- the host's ClickHouse server). Idempotent like every other init statement.

CREATE DATABASE IF NOT EXISTS tms;

CREATE TABLE IF NOT EXISTS tms.tx_contract_anomaly
(
    network         String,
    tx_hash         String,
    target          String,            -- watched contract this verdict is for
    cluster_id      Int32,             -- -1 = online noise / unassigned
    iso_score       Float64,           -- Isolation Forest (evidence)
    lof_score       Float64,           -- Local Outlier Factor (evidence)
    consensus       Float64,           -- [0,1] ensemble consensus (NaN-safe)
    votes           UInt8,             -- 0..3 detector votes
    verdict         String,            -- malicious | benign | anomaly | normal
    model_id        String,            -- the frozen ShapeModel that scored it
    feature_set     String,            -- shape | graph | combined
    evidence        String DEFAULT '{}',  -- JSON: top deviating features, etc.
    scored_at       DateTime DEFAULT now(),     -- SOURCE time: the run/classify that produced the verdict
    published_at    DateTime64(6) DEFAULT now64(6)  -- RECONCILIATION version: see below
)
-- Versioned by `published_at`, NOT `scored_at`. Every reconciliation (publish,
-- relabel, clear, delete) stamps a monotonic `published_at`, so the LATEST
-- reconciliation always wins on FINAL even when it re-publishes a positive whose
-- SOURCE time (scored_at, from the original run/classify) is older than a prior
-- tombstone. Versioning on scored_at instead would let a `now()` tombstone keep
-- beating a re-published positive after a benign label is CLEARED, hiding the
-- re-raised alert until a future fit produced a newer scored_at. DateTime64(6)
-- (microsecond) avoids same-second version ties between back-to-back syncs.
ENGINE = ReplacingMergeTree(published_at)
ORDER BY (network, tx_hash, target);
