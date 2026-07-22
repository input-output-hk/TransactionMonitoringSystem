-- 010: per-contract "latest N to cluster on" (the read/fit/count window).
--
-- Distinct from `requested_max_txs`, which stays the backfill DOWNLOAD depth it
-- has always been. `target_txs` is the number of most-recent transactions a
-- contract is clustered/scored/counted over (the rolling read window). It is a
-- SEPARATE column on purpose: `requested_max_txs` already held values under the
-- old "download depth" meaning, so reinterpreting it as the read window would
-- silently shrink the fit of every existing contract on deploy. A new column
-- defaults to 0 for all pre-existing rows, and 0 resolves to the window ceiling
-- (CLUSTERING_WINDOW_TXS) in Settings.effective_window_txs, so the deploy leaves
-- every already-onboarded contract's window exactly where it was (recall first).
-- An operator sets a smaller N deliberately, after which it governs the window.
ALTER TABLE tms.contracts
    ADD COLUMN IF NOT EXISTS target_txs UInt32 DEFAULT 0 AFTER requested_max_txs;
