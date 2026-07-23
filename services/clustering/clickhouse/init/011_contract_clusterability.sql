-- 011: per-contract clusterability of the frozen fit, and the last-fit timestamp.
--
-- Both columns exist to tell two facts apart that the online-noise rate
-- ("drift_score", 008) alone cannot:
--   * `fit_coverage` = the fraction of the fit's OWN training window that landed
--     in some cluster (1 - n_noise/n_points), written by process_contract at the
--     end of a fit. A value below MIN_CLUSTER_COVERAGE means the shape does not
--     cluster at the auto-selected DBSCAN params: the model failed to describe its
--     own data, so a high drift_score is STRUCTURAL (a re-cluster reproduces the
--     same DBSCAN-noise) rather than staleness. The scheduler and the API use it
--     to stop recommending/auto-running a futile re-cluster. Sentinel -1 = "not
--     yet fit" (rows written before this migration, or contracts with too few txs
--     to cluster): treated as clusterable so they behave exactly as before 011
--     until their first fit records a real coverage.
--   * `last_fit_at` = unix seconds of the last SYSTEM fit (0 = never). Drives the
--     anti-flap / re-baseline cadence (FEED_REFIT_MIN_INTERVAL_SECONDS) so one
--     contract cannot be auto-re-fit on every poll. UInt32 for trivial interval
--     arithmetic in the scheduler.
--
-- Additive and idempotent (ADD COLUMN IF NOT EXISTS), mirroring 010: pre-existing
-- rows keep their behaviour on deploy (recall first).

ALTER TABLE tms.contracts
    ADD COLUMN IF NOT EXISTS fit_coverage Float64 DEFAULT -1 AFTER drift_score;

ALTER TABLE tms.contracts
    ADD COLUMN IF NOT EXISTS last_fit_at UInt32 DEFAULT 0 AFTER fit_coverage;
