-- 012: mark contract_anomaly rows that came from an un-clusterable fit.
--
-- `unclusterable_fit` = 1 when the row's contract has a frozen fit with no usable
-- cluster structure (fit_coverage < MIN_CLUSTER_COVERAGE, see migration 011). Such
-- a contract's "anomaly" verdicts are outlier-detector signals against an
-- un-clusterable baseline, not cluster-relative anomalies, and they arrive as a
-- high-volume degenerate-window flood. This column lets the host feed GROUP and
-- de-prioritize that flood into an honest "no stable clusters; N low-confidence
-- outliers" summary.
--
-- It is EVIDENCE ONLY, never a suppression flag: every flagged row is still
-- published, queryable, corroboration-eligible and individually inspectable, so
-- recall is untouched (a mismarked row is still fully visible). The publisher
-- derives it from the SAME per-contract coverage the scheduler/UI use (one
-- signal, no second threshold to diverge), stamped uniformly on every row a
-- reconciliation writes (positives and tombstones alike).
--
-- Additive and idempotent (ADD COLUMN IF NOT EXISTS), mirroring 011: pre-existing
-- rows default to 0 (treated clusterable) until the next publish re-stamps them.

ALTER TABLE tms.tx_contract_anomaly
    ADD COLUMN IF NOT EXISTS unclusterable_fit UInt8 DEFAULT 0 AFTER feature_set;
