-- Source-owned ingest cursor (portability seam for non-Blockfrost adapters).
--
-- `cursor` is an opaque-but-tagged string owned by the chain source
-- (Blockfrost: 'page:N'; a node adapter: 'point:<slot>.<block_hash>').
-- `source` records WHICH adapter produced it, so a cursor is never replayed
-- into a different provider. `last_page` is dead but kept (cheap, and dropping
-- columns buys nothing); new rows leave it at 0.
--
-- Convention for all migrations: every statement must be idempotent
-- (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / guarded UPDATE), because
-- `python -m app.cli migrate` re-applies every init file in order.

ALTER TABLE tms.ingest_cursor ADD COLUMN IF NOT EXISTS cursor String DEFAULT '';
ALTER TABLE tms.ingest_cursor ADD COLUMN IF NOT EXISTS source String DEFAULT 'blockfrost';

-- Backfill pre-006 rows with the Blockfrost encoding of their page number.
-- Guarded on cursor='' so re-running is a no-op; one row per target, so the
-- mutation is trivial.
ALTER TABLE tms.ingest_cursor UPDATE cursor = concat('page:', toString(last_page))
    WHERE cursor = '' AND last_page > 0 SETTINGS mutations_sync = 2;
