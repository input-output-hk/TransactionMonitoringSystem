-- 008: per-contract online-classifier drift score.
--
-- `drift_score` is the trailing "online-noise rate" — the fraction of recently
-- classified txs that fall outside every frozen cluster (cluster_id == -1),
-- written by service.update_contract at the end of each incremental classify run.
-- The API derives `reclustering_suggested` from it against RECLUSTER_NOISE_THRESHOLD;
-- storing the raw rate (not the boolean) lets the threshold be retuned without
-- re-running jobs. Pre-existing rows default to 0 (no drift observed yet).

ALTER TABLE tms.contracts
    ADD COLUMN IF NOT EXISTS drift_score Float64 DEFAULT 0;
