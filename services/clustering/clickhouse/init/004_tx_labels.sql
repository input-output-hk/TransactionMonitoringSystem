-- Manual verdict labels applied to individual transactions.
-- Auto-loaded on first container start (alongside 001/002/003); apply by hand to
-- an existing volume (see docs/data-model.md).
--
-- A "label this cluster" action writes one row per current cluster member; future
-- members inherit the verdict at display time via cluster-membership inheritance.
-- Keyed on the stable tx_hash (NOT run_id/cluster_id, which are ephemeral per run),
-- so labels survive reprocessing. "Clearing" a label inserts a deleted=1 tombstone
-- (append-only; matches the ReplacingMergeTree pattern used elsewhere — no async
-- ALTER ... DELETE). Reads use FINAL and filter deleted = 0.

CREATE DATABASE IF NOT EXISTS tms;

CREATE TABLE IF NOT EXISTS tms.tx_labels
(
    target      String,
    tx_hash     FixedString(64),
    label       Enum8('malicious' = 1, 'benign' = 2),
    source      Enum8('cluster' = 1, 'manual_tx' = 2) DEFAULT 'cluster',
    deleted     UInt8 DEFAULT 0,           -- tombstone; "clear" inserts deleted = 1
    note        String DEFAULT '',
    updated_at  DateTime64(6) DEFAULT now64(6)  -- newest write wins
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (target, tx_hash);
