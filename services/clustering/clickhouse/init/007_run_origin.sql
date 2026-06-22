-- 007: backfill the run `origin` column on volumes created before it existed.
--
-- `origin` was added by editing the CREATE TABLE statements in 001/002 instead
-- of an ALTER migration, so volumes initialized before then never got it
-- (CREATE TABLE IF NOT EXISTS is a no-op on existing tables) and every
-- cluster_runs/anomaly_runs read 500s. Pre-existing rows default to 'custom':
-- there is no way to know retroactively which run was the canonical auto-tuned
-- one, and `process_contract --reprocess` recreates a 'system' run on demand.

ALTER TABLE tms.cluster_runs
    ADD COLUMN IF NOT EXISTS origin Enum8('system' = 1, 'custom' = 2) DEFAULT 'custom';

ALTER TABLE tms.anomaly_runs
    ADD COLUMN IF NOT EXISTS origin Enum8('system' = 1, 'custom' = 2) DEFAULT 'custom';
