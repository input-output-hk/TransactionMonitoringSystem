"""PostgreSQL database connection and operations"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
import asyncpg
from contextlib import asynccontextmanager

from app.config import settings

logger = logging.getLogger(__name__)

# Global connection pool
_pool: Optional[asyncpg.Pool] = None
# Serializes pool creation: without it two concurrent init_pool() callers could
# both observe `_pool is None`, both create_pool(), and one pool would leak
# (its connections never closed). asyncio.Lock() needs no running loop to
# construct on the supported Python versions.
_pool_init_lock = asyncio.Lock()


def _affected_rows(command_tag: str) -> int:
    """Row count from an asyncpg command tag. asyncpg returns the SQL command tag
    string ('UPDATE 3', 'DELETE 12') for non-SELECT statements; the affected-row
    count is its last whitespace-separated token."""
    return int(command_tag.split()[1])


async def init_pool():
    """Initialize PostgreSQL connection pool (idempotent, concurrency-safe)."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_init_lock:
        # Re-check inside the lock: a racing caller may have created it while we
        # waited, so we must not build (and leak) a second pool.
        if _pool is not None:
            return _pool
        try:
            _pool = await asyncpg.create_pool(
                host=settings.POSTGRES_HOST,
                port=settings.POSTGRES_PORT,
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD,
                database=settings.POSTGRES_DB,
                min_size=settings.POSTGRES_POOL_MIN_SIZE,
                max_size=settings.POSTGRES_POOL_MAX_SIZE,
                # Recycle connections idle beyond this so a PG restart or
                # transient network blip doesn't leave stale sockets in the pool.
                max_inactive_connection_lifetime=settings.POSTGRES_POOL_MAX_IDLE_SECONDS,
                # Cap any single statement so a stuck query can't pin a pool
                # slot indefinitely.
                command_timeout=settings.POSTGRES_STATEMENT_TIMEOUT_SECONDS,
            )
            logger.info("PostgreSQL connection pool initialized")
        except Exception as e:
            logger.error(f"Failed to initialize PostgreSQL pool: {e}")
            raise
    return _pool


async def close_pool():
    """Close PostgreSQL connection pool"""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL connection pool closed")


@asynccontextmanager
async def get_connection():
    """Get a database connection from the pool"""
    if _pool is None:
        await init_pool()
    async with _pool.acquire() as conn:
        yield conn


async def execute_schema():
    """Create database schema if it doesn't exist"""
    async with get_connection() as conn:
        # Create tables for config, metadata, audit logs, users
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key VARCHAR(255) PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                id SERIAL PRIMARY KEY,
                entity_type VARCHAR(100) NOT NULL,
                entity_id VARCHAR(255) NOT NULL,
                key VARCHAR(255) NOT NULL,
                value TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(entity_type, entity_id, key)
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id SERIAL PRIMARY KEY,
                event_type VARCHAR(100) NOT NULL,
                user_id INTEGER REFERENCES users(id),
                entity_type VARCHAR(100),
                entity_id VARCHAR(255),
                action VARCHAR(100) NOT NULL,
                details JSONB,
                ip_address INET,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS idx_metadata_entity ON metadata(entity_type, entity_id);
        """)

        # Transaction Lifecycle table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tx_lifecycle (
                tx_id           TEXT PRIMARY KEY,
                network         TEXT NOT NULL DEFAULT 'preprod',
                status          TEXT NOT NULL,
                first_seen_at   TIMESTAMPTZ,
                confirmed_at    TIMESTAMPTZ,
                rolled_back_at  TIMESTAMPTZ,
                block_hash      TEXT,
                slot            BIGINT,
                height          BIGINT,
                latency_ms      BIGINT,
                created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migrate existing TIMESTAMP columns to TIMESTAMPTZ.
        # PostgreSQL treats bare TIMESTAMP values as local time during the cast;
        # Docker containers default to UTC so no data is shifted.
        try:
            await conn.execute("""
                ALTER TABLE tx_lifecycle
                    ALTER COLUMN first_seen_at  TYPE TIMESTAMPTZ,
                    ALTER COLUMN confirmed_at   TYPE TIMESTAMPTZ,
                    ALTER COLUMN rolled_back_at TYPE TIMESTAMPTZ,
                    ALTER COLUMN created_at     TYPE TIMESTAMPTZ,
                    ALTER COLUMN updated_at     TYPE TIMESTAMPTZ
            """)
        except Exception:
            pass  # columns already TIMESTAMPTZ — no-op

        # Add dropped_at column for DROPPED lifecycle state (idempotent).
        try:
            await conn.execute("""
                ALTER TABLE tx_lifecycle ADD COLUMN IF NOT EXISTS dropped_at TIMESTAMPTZ
            """)
        except Exception:
            pass

        # Drop raw_event column — raw blobs now stored in local filesystem raw store (ADR-009).
        # raw_event was write-only (never read by any API or analysis query).
        try:
            await conn.execute("""
                ALTER TABLE tx_lifecycle DROP COLUMN IF EXISTS raw_event
            """)
        except Exception:
            pass

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tx_lifecycle_status ON tx_lifecycle(status);
            CREATE INDEX IF NOT EXISTS idx_tx_lifecycle_network ON tx_lifecycle(network);
            CREATE INDEX IF NOT EXISTS idx_tx_lifecycle_first_seen ON tx_lifecycle(first_seen_at);
            CREATE INDEX IF NOT EXISTS idx_tx_lifecycle_confirmed ON tx_lifecycle(confirmed_at);
        """)

        # Partial composite index that makes the PENDING → DROPPED cleanup sweep fast.
        # Covers: WHERE network = $1 AND status = 'PENDING' AND first_seen_at < threshold
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tx_lifecycle_pending_cleanup
            ON tx_lifecycle (network, first_seen_at)
            WHERE status = 'PENDING'
        """)

        # Sync checkpoint — one row per network, upserted after each block
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_checkpoint (
                network     TEXT PRIMARY KEY,
                slot        BIGINT NOT NULL,
                block_id    TEXT NOT NULL,
                updated_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Entity state — arbitrary JSON blobs keyed by (network, entity_type, entity_id)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_state (
                network      TEXT NOT NULL,
                entity_type  TEXT NOT NULL,
                entity_id    TEXT NOT NULL,
                state        JSONB NOT NULL,
                updated_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (network, entity_type, entity_id)
            )
        """)

        # Mempool collision tracking for front-running detection
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mempool_collisions (
                id SERIAL PRIMARY KEY,
                tx_a TEXT NOT NULL,
                tx_b TEXT NOT NULL,
                network TEXT NOT NULL,
                shared_inputs JSONB NOT NULL,
                shared_count INT NOT NULL,
                tx_a_seen_at TIMESTAMPTZ NOT NULL,
                tx_b_seen_at TIMESTAMPTZ NOT NULL,
                delta_ms DOUBLE PRECISION,
                outcome TEXT DEFAULT 'BOTH_PENDING',
                tx_a_fee BIGINT,
                tx_b_fee BIGINT,
                tx_a_first_input_addr TEXT DEFAULT '',
                tx_b_first_input_addr TEXT DEFAULT '',
                tx_a_ttl INT DEFAULT 0,
                tx_b_ttl INT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_mempool_collisions_tx_a
                ON mempool_collisions(tx_a);
            CREATE INDEX IF NOT EXISTS idx_mempool_collisions_tx_b
                ON mempool_collisions(tx_b);
            CREATE INDEX IF NOT EXISTS idx_mempool_collisions_network
                ON mempool_collisions(network);
            -- Supports the 24h-window filter in ``get_collisions_for_txs``.
            -- Without it the conditional-aggregation FILTER does a full
            -- table scan; cheap on preprod but expensive at mainnet scale.
            CREATE INDEX IF NOT EXISTS idx_mempool_collisions_created_at
                ON mempool_collisions(created_at);
        """)

        # Durable queue for the delayed tx_class_scores rollback repurge.
        # The in-memory asyncio task that performs the delayed second purge
        # pass is volatile (lost on restart/shutdown inside the delay
        # window); a lost repurge leaves a stale score row that permanently
        # blocks re-scoring of a re-confirmed tx (missed-attack risk). Rows
        # are written BEFORE the task is scheduled and deleted only after
        # the ClickHouse delete succeeds; chain-sync (re)connect replays
        # whatever is left.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_score_repurges (
                network     TEXT NOT NULL,
                tx_hash     TEXT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (network, tx_hash)
            )
        """)

        # Notification de-duplication ledger. tx_class_scores
        # is a ReplacingMergeTree and the engine re-scores the same tx (full
        # rescans, raw-data deferrals resolving later), so a tx surfaces as
        # "newly scored" repeatedly. band_rank records the highest band already
        # notified; a re-score notifies again ONLY on escalation to a higher
        # band. See claim_notification().
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS notified_alerts (
                network     TEXT NOT NULL,
                tx_hash     TEXT NOT NULL,
                band_rank   SMALLINT NOT NULL,
                notified_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (network, tx_hash)
            )
        """)

        # Periodic-report scheduling state. One row per
        # (network, report_kind); last_sent_at is the boundary the scheduler
        # checks so a restart neither double-sends nor skips a period.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS notification_report_state (
                network           TEXT NOT NULL,
                report_kind       TEXT NOT NULL DEFAULT 'periodic',
                last_sent_at      TIMESTAMPTZ,
                last_window_start TIMESTAMPTZ,
                last_window_end   TIMESTAMPTZ,
                PRIMARY KEY (network, report_kind)
            )
        """)

        logger.info("PostgreSQL schema initialized")


# --- Notification de-duplication ---

# Band rank: a higher rank supersedes a lower one, so an escalation
# (e.g. High -> Critical) re-notifies while a same/lower re-score does not.
# WHICH bands page at all is decided by the trigger config, not here.
_BAND_RANK = {"Informational": 0, "Moderate": 1, "High": 2, "Critical": 3}


async def claim_notification(network: str, tx_hash: str, band: str) -> bool:
    """Atomically claim the right to notify for (network, tx_hash) at ``band``.

    Returns True only when this is the FIRST notification for the transaction
    or a genuine escalation to a higher band than was previously notified; a
    same-or-lower re-score returns False. Race-free: concurrent callers
    serialize on the primary key, so exactly one wins the claim.

    The single statement does the check-and-claim atomically. A row is
    RETURNED iff we inserted a fresh row (``xmax = 0``) or the conflict's
    ``WHERE band_rank < EXCLUDED.band_rank`` matched (escalation). When the
    guard is false (same/lower band) the UPDATE is a no-op and NO row is
    returned — that is the duplicate-suppression signal.
    """
    rank = _BAND_RANK.get(band, -1)
    if rank < 0:
        return False  # unknown band — never notify
    async with get_connection() as conn:
        row = await conn.fetchrow("""
            INSERT INTO notified_alerts (network, tx_hash, band_rank)
            VALUES ($1, $2, $3)
            ON CONFLICT (network, tx_hash) DO UPDATE
                SET band_rank = EXCLUDED.band_rank,
                    notified_at = CURRENT_TIMESTAMP
                WHERE notified_alerts.band_rank < EXCLUDED.band_rank
            RETURNING (xmax = 0) AS inserted
        """, network, tx_hash, rank)
    return row is not None


async def prune_notified_alerts(older_than_days: int) -> int:
    """Delete dedup-ledger rows older than the retention window.

    Re-notification suppression only needs a recent window (a tx old enough not
    to be re-scored no longer needs its row), so this bounds notified_alerts
    growth — the table is otherwise one append-only row per alerted tx forever.
    NOTIFY_DEDUP_RETENTION_DAYS=0 disables.
    """
    async with get_connection() as conn:
        result = await conn.execute("""
            DELETE FROM notified_alerts
            WHERE notified_at < NOW() - ($1 * INTERVAL '1 day')
        """, older_than_days)
        return int(result.split()[1])


async def get_report_state(
    network: str, report_kind: str = "periodic",
) -> Optional[Dict[str, Any]]:
    """Last-sent bookkeeping for the periodic report, or None if never sent."""
    async with get_connection() as conn:
        row = await conn.fetchrow("""
            SELECT last_sent_at, last_window_start, last_window_end
            FROM notification_report_state
            WHERE network = $1 AND report_kind = $2
        """, network, report_kind)
    return dict(row) if row else None


async def mark_report_sent(
    network: str, window_start: datetime, window_end: datetime,
    sent_at: datetime, report_kind: str = "periodic",
) -> None:
    """Advance the report boundary after a successful send (idempotent upsert)."""
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO notification_report_state
                (network, report_kind, last_sent_at, last_window_start, last_window_end)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (network, report_kind) DO UPDATE SET
                last_sent_at      = EXCLUDED.last_sent_at,
                last_window_start = EXCLUDED.last_window_start,
                last_window_end   = EXCLUDED.last_window_end
        """, network, report_kind, sent_at, window_start, window_end)


# --- Transaction Lifecycle CRUD ---

async def upsert_lifecycle_pending(tx_id: str, network: str, first_seen_at: datetime):
    """Insert a transaction as PENDING (first seen in mempool).

    Raw payload is written to the local filesystem raw store (ADR-009), not here.
    """
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO tx_lifecycle (tx_id, network, status, first_seen_at)
            VALUES ($1, $2, 'PENDING', $3)
            ON CONFLICT (tx_id) DO NOTHING
        """, tx_id, network, first_seen_at)


async def upsert_lifecycle_confirmed(tx_id: str, network: str, confirmed_at: datetime,
                                      block_hash: str, slot: int, height: int):
    """Mark a transaction as CONFIRMED, computing latency if previously PENDING.

    Single-row model: tx_id is the primary key.  If the transaction was previously
    ROLLED_BACK and has now been re-confirmed at a different block, the row is updated
    in place to the new CONFIRMED state and rolled_back_at is reset to NULL — the
    previous rollback is no longer the canonical state of this transaction.
    """
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO tx_lifecycle (tx_id, network, status, confirmed_at, block_hash, slot, height)
            VALUES ($1, $2, 'CONFIRMED', $3, $4, $5, $6)
            ON CONFLICT (tx_id) DO UPDATE SET
                status       = 'CONFIRMED',
                confirmed_at = $3,
                block_hash   = $4,
                slot         = $5,
                height       = $6,
                rolled_back_at = NULL,
                latency_ms   = CASE
                    WHEN tx_lifecycle.first_seen_at IS NOT NULL
                    THEN EXTRACT(EPOCH FROM ($3 - tx_lifecycle.first_seen_at)) * 1000
                    ELSE NULL
                END,
                updated_at   = CURRENT_TIMESTAMP
        """, tx_id, network, confirmed_at, block_hash, slot, height)


async def batch_upsert_lifecycle_confirmed(records: List[tuple]):
    """Batch-upsert confirmed transactions in a single executemany call.

    Each record is (tx_id, network, confirmed_at, block_hash, slot, height).
    Reduces N sequential round-trips to the pool to one prepared-statement batch.
    rolled_back_at is reset to NULL on re-confirmation (see upsert_lifecycle_confirmed).
    """
    if not records:
        return
    async with get_connection() as conn:
        await conn.executemany("""
            INSERT INTO tx_lifecycle (tx_id, network, status, confirmed_at, block_hash, slot, height)
            VALUES ($1, $2, 'CONFIRMED', $3, $4, $5, $6)
            ON CONFLICT (tx_id) DO UPDATE SET
                status         = 'CONFIRMED',
                confirmed_at   = $3,
                block_hash     = $4,
                slot           = $5,
                height         = $6,
                rolled_back_at = NULL,
                latency_ms     = CASE
                    WHEN tx_lifecycle.first_seen_at IS NOT NULL
                    THEN EXTRACT(EPOCH FROM ($3 - tx_lifecycle.first_seen_at)) * 1000
                    ELSE NULL
                END,
                updated_at     = CURRENT_TIMESTAMP
        """, records)


async def mark_lifecycle_rolled_back(rollback_slot: int, network: str):
    """Mark all transactions confirmed after rollback_slot as ROLLED_BACK.

    Triggered when Ogmios sends a rollBackward event whose target point has
    slot S.  Every CONFIRMED transaction whose slot > S was in a block that
    is no longer part of the canonical chain and must be re-evaluated.

    Single-row model: tx_lifecycle has one row per tx_hash.  A re-confirmed
    transaction (ROLLED_BACK → CONFIRMED after resubmission) overwrites this
    row; rolled_back_at is reset to NULL on re-confirmation so the row always
    reflects the current canonical state.
    """
    async with get_connection() as conn:
        result = await conn.execute("""
            UPDATE tx_lifecycle
            SET status = 'ROLLED_BACK',
                rolled_back_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE network = $1
              AND slot > $2
              AND status = 'CONFIRMED'
        """, network, rollback_slot)
        return result


async def get_lifecycle_by_tx_id(tx_id: str) -> Optional[Dict[str, Any]]:
    """Get lifecycle record for a single transaction"""
    async with get_connection() as conn:
        row = await conn.fetchrow("SELECT * FROM tx_lifecycle WHERE tx_id = $1", tx_id)
        if row:
            return dict(row)
        return None


async def get_lifecycles_by_status(status: str, network: str = "preprod",
                                    limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Query lifecycle records by status"""
    async with get_connection() as conn:
        rows = await conn.fetch("""
            SELECT * FROM tx_lifecycle
            WHERE status = $1 AND network = $2
            ORDER BY created_at DESC
            LIMIT $3 OFFSET $4
        """, status, network, limit, offset)
        return [dict(r) for r in rows]


async def get_all_lifecycles(network: str = "preprod",
                              limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Query all lifecycle records regardless of status"""
    async with get_connection() as conn:
        rows = await conn.fetch("""
            SELECT * FROM tx_lifecycle
            WHERE network = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
        """, network, limit, offset)
        return [dict(r) for r in rows]


async def get_lifecycle_summary(network: str = "preprod") -> Dict[str, Any]:
    """Get aggregate lifecycle statistics"""
    async with get_connection() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total_tracked,
                COUNT(*) FILTER (WHERE status = 'PENDING')     AS pending_count,
                COUNT(*) FILTER (WHERE status = 'CONFIRMED')   AS confirmed_count,
                COUNT(*) FILTER (WHERE status = 'ROLLED_BACK') AS rolled_back_count,
                COUNT(*) FILTER (WHERE status = 'DROPPED')     AS dropped_count,
                AVG(latency_ms) FILTER (WHERE latency_ms IS NOT NULL) AS avg_latency_ms
            FROM tx_lifecycle
            WHERE network = $1
        """, network)
        result = dict(row)
        total = result["total_tracked"]
        result["rollback_rate"] = (result["rolled_back_count"] / total * 100) if total > 0 else 0.0
        return result


# --- Sync Checkpoint ---

async def save_sync_point(network: str, slot: int, block_id: str):
    """Persist the last successfully processed chain sync point."""
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO sync_checkpoint (network, slot, block_id, updated_at)
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (network) DO UPDATE SET
                slot       = $2,
                block_id   = $3,
                updated_at = CURRENT_TIMESTAMP
        """, network, slot, block_id)


async def get_sync_point(network: str) -> Optional[Dict[str, Any]]:
    """Return the last saved sync point for the given network, or None on first run."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT slot, block_id FROM sync_checkpoint WHERE network = $1", network
        )
        if row:
            return {"slot": row["slot"], "id": row["block_id"]}
        return None


# --- Pending score repurges (durable rollback second pass) ---

async def add_pending_score_repurges(network: str, tx_hashes: List[str]) -> None:
    """Persist tx hashes awaiting the delayed tx_class_scores repurge.

    Written BEFORE the in-memory repurge task is scheduled so a restart or
    shutdown inside the delay window cannot lose the repurge. ON CONFLICT
    DO NOTHING: a rollback re-delivered by the node (the cleanup path is
    idempotent) may enqueue the same hashes twice.
    """
    if not tx_hashes:
        return
    async with get_connection() as conn:
        await conn.executemany("""
            INSERT INTO pending_score_repurges (network, tx_hash)
            VALUES ($1, $2)
            ON CONFLICT (network, tx_hash) DO NOTHING
        """, [(network, h) for h in tx_hashes])


async def get_pending_score_repurges(network: str) -> List[str]:
    """All tx hashes whose delayed score repurge has not completed yet."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            "SELECT tx_hash FROM pending_score_repurges WHERE network = $1",
            network,
        )
        return [r["tx_hash"] for r in rows]


async def clear_pending_score_repurges(network: str, tx_hashes: List[str]) -> None:
    """Remove hashes whose tx_class_scores repurge succeeded in ClickHouse.

    Called only AFTER delete_score_rows_async returns: clearing first would
    reopen the lost-repurge window this table exists to close.
    """
    if not tx_hashes:
        return
    async with get_connection() as conn:
        await conn.execute("""
            DELETE FROM pending_score_repurges
            WHERE network = $1 AND tx_hash = ANY($2)
        """, network, tx_hashes)


# --- Entity State ---

async def get_entity_state(entity_type: str, entity_id: str, network: str) -> Optional[Dict[str, Any]]:
    """Return the JSON state for a given entity, or None if not found."""
    async with get_connection() as conn:
        row = await conn.fetchrow("""
            SELECT state FROM entity_state
            WHERE network = $1 AND entity_type = $2 AND entity_id = $3
        """, network, entity_type, entity_id)
        if row:
            return json.loads(row["state"])
        return None


async def set_entity_state(entity_type: str, entity_id: str, state: Dict[str, Any], network: str):
    """Upsert the JSON state for a given entity."""
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO entity_state (network, entity_type, entity_id, state, updated_at)
            VALUES ($1, $2, $3, $4::jsonb, CURRENT_TIMESTAMP)
            ON CONFLICT (network, entity_type, entity_id) DO UPDATE SET
                state      = $4::jsonb,
                updated_at = CURRENT_TIMESTAMP
        """, network, entity_type, entity_id, json.dumps(state))


# --- Lifecycle cleanup ---

async def mark_dropped_pending_txs(network: str, older_than_seconds: int) -> int:
    """Mark stale PENDING transactions as DROPPED.

    A transaction is considered stale when it has been PENDING for longer than
    older_than_seconds without being confirmed or rolled back.  The partial index
    idx_tx_lifecycle_pending_cleanup makes this query a fast index scan.

    Returns the number of rows updated.
    """
    async with get_connection() as conn:
        result = await conn.execute("""
            UPDATE tx_lifecycle
            SET status     = 'DROPPED',
                dropped_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE network       = $1
              AND status        = 'PENDING'
              AND first_seen_at < NOW() - ($2 * INTERVAL '1 second')
        """, network, older_than_seconds)
        return _affected_rows(result)


async def insert_audit_log(
    event_type: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: str,
    ip_address: Optional[str],
) -> int:
    """Append one audit row and return its id. ``details`` is a JSON string;
    ``user_id`` stays NULL until server-side accounts exist (the actor is
    carried in details).
    """
    # Defence-in-depth: app.net.client_ip already validates, but the ::inet
    # cast aborts the whole insert on any malformed value reaching this
    # layer, and for fail-closed audit callers that would block the action.
    from app.net import parse_ip

    ip_address = parse_ip(ip_address)
    async with get_connection() as conn:
        return await conn.fetchval("""
            INSERT INTO audit_logs (
                event_type, entity_type, entity_id, action, details, ip_address
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6::inet)
            RETURNING id
        """, event_type, entity_type, entity_id, action, details, ip_address)


async def update_audit_log_details(audit_id: int, outcome: str) -> None:
    """Merge ``outcome`` (a JSON object string) into an audit row's details."""
    async with get_connection() as conn:
        await conn.execute("""
            UPDATE audit_logs SET details = details || $2::jsonb WHERE id = $1
        """, audit_id, outcome)


async def prune_terminal_lifecycle(network: str, older_than_days: int) -> int:
    """Delete TERMINAL lifecycle rows (DROPPED / ROLLED_BACK) older than the
    retention window. CONFIRMED and PENDING rows are never pruned: CONFIRMED
    is the canonical lifecycle record and PENDING is live state. Retention
    is opt-in (LIFECYCLE_RETENTION_DAYS=0 keeps everything).
    """
    async with get_connection() as conn:
        result = await conn.execute("""
            DELETE FROM tx_lifecycle
            WHERE network    = $1
              AND status IN ('DROPPED', 'ROLLED_BACK')
              AND updated_at < NOW() - ($2 * INTERVAL '1 day')
        """, network, older_than_days)
        return _affected_rows(result)


async def prune_audit_logs(older_than_days: int) -> int:
    """Delete audit rows older than the retention window.

    No network column on audit_logs (actions are instance-scoped); uses
    idx_audit_logs_created_at. Opt-in: AUDIT_LOG_RETENTION_DAYS=0 keeps
    everything (audit rows are the suppression accountability record).
    """
    async with get_connection() as conn:
        result = await conn.execute("""
            DELETE FROM audit_logs
            WHERE created_at < NOW() - ($1 * INTERVAL '1 day')
        """, older_than_days)
        return _affected_rows(result)


async def prune_mempool_collisions(network: str, older_than_days: int) -> int:
    """Delete collision records older than the retention window.

    Note the precision trade-off before enabling: the front_running
    attacker-recurrence signal counts wins over collision HISTORY, so
    pruning shrinks the window that signal can see. Opt-in
    (MEMPOOL_COLLISION_RETENTION_DAYS=0 keeps everything).
    """
    async with get_connection() as conn:
        result = await conn.execute("""
            DELETE FROM mempool_collisions
            WHERE network    = $1
              AND created_at < NOW() - ($2 * INTERVAL '1 day')
        """, network, older_than_days)
        return _affected_rows(result)


# --- Mempool Collision Tracking (Front-Running Detection) ---

async def insert_mempool_collision(
    tx_a: str, tx_b: str, network: str,
    shared_inputs: list, shared_count: int,
    tx_a_seen_at: datetime, tx_b_seen_at: datetime,
    delta_ms: float,
    tx_a_fee: int = 0, tx_b_fee: int = 0,
    tx_a_first_input_addr: str = "",
    tx_b_first_input_addr: str = "",
    tx_a_ttl: int = 0, tx_b_ttl: int = 0,
):
    """Record a mempool collision between two transactions sharing inputs."""
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO mempool_collisions
                (tx_a, tx_b, network, shared_inputs, shared_count,
                 tx_a_seen_at, tx_b_seen_at, delta_ms, tx_a_fee, tx_b_fee,
                 tx_a_first_input_addr, tx_b_first_input_addr,
                 tx_a_ttl, tx_b_ttl)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14)
        """, tx_a, tx_b, network, json.dumps(shared_inputs), shared_count,
            tx_a_seen_at, tx_b_seen_at, delta_ms, tx_a_fee, tx_b_fee,
            tx_a_first_input_addr, tx_b_first_input_addr,
            tx_a_ttl, tx_b_ttl)


async def get_collisions_for_txs(tx_hashes: list, network: str) -> Dict[str, Dict[str, Any]]:
    """Fetch collision data for a batch of tx hashes. Returns {tx_hash: collision_dict}."""
    if not tx_hashes:
        return {}
    async with get_connection() as conn:
        rows = await conn.fetch("""
            SELECT tx_a, tx_b, shared_count, delta_ms, outcome,
                   tx_a_fee, tx_b_fee,
                   tx_a_first_input_addr, tx_b_first_input_addr,
                   tx_a_ttl, tx_b_ttl
            FROM mempool_collisions
            WHERE network = $1
              AND (tx_a = ANY($2) OR tx_b = ANY($2))
        """, network, tx_hashes)

    # Pre-compute attacker win counts for all counterpart addresses in one query
    counterpart_addrs = set()
    for r in rows:
        counterpart_addrs.add(r["tx_a_first_input_addr"])
        counterpart_addrs.add(r["tx_b_first_input_addr"])
    counterpart_addrs.discard("")

    win_counts: Dict[str, int] = {}
    win_counts_24h: Dict[str, int] = {}
    if counterpart_addrs:
        async with get_connection() as conn:
            # Single query covers both the all-time count and the 24-hour
            # count via conditional aggregation. Halves the Postgres
            # round-trip vs. running two near-identical UNION ALL queries.
            #
            # Semantic note: ``created_at`` is the **mempool detection
            # time** (when this row was inserted by the ingester),
            # NOT the outcome confirmation time. So
            # ``attacker_win_count_24h`` means "wins on collisions first
            # observed in the last 24 hours" — operationally this tracks
            # recent attacker-cluster activity in the race window, which
            # is the signal operators triage on. To filter on
            # confirmation time instead would require an additional
            # ``confirmed_at`` column populated by
            # :func:`update_collision_outcome`.
            win_rows = await conn.fetch("""
                SELECT addr,
                       COUNT(*) AS cnt,
                       COUNT(*) FILTER (
                           WHERE created_at >= NOW() - INTERVAL '24 hours'
                       ) AS cnt_24h
                FROM (
                    SELECT tx_a_first_input_addr AS addr, created_at
                    FROM mempool_collisions
                    WHERE network = $1 AND outcome = 'TX_A_CONFIRMED'
                      AND tx_a_first_input_addr = ANY($2)
                    UNION ALL
                    SELECT tx_b_first_input_addr AS addr, created_at
                    FROM mempool_collisions
                    WHERE network = $1 AND outcome = 'TX_B_CONFIRMED'
                      AND tx_b_first_input_addr = ANY($2)
                ) sub
                GROUP BY addr
            """, network, list(counterpart_addrs))
            win_counts = {r["addr"]: r["cnt"] for r in win_rows}
            win_counts_24h = {r["addr"]: r["cnt_24h"] for r in win_rows}

    result: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        for tx_hash in [r["tx_a"], r["tx_b"]]:
            if tx_hash in tx_hashes and tx_hash not in result:
                is_a = tx_hash == r["tx_a"]
                counterpart = r["tx_b"] if is_a else r["tx_a"]
                counterpart_addr = r["tx_b_first_input_addr"] if is_a else r["tx_a_first_input_addr"]
                my_addr = r["tx_a_first_input_addr"] if is_a else r["tx_b_first_input_addr"]
                counterpart_ttl = r["tx_b_ttl"] if is_a else r["tx_a_ttl"]
                result[tx_hash] = {
                    "counterpart_tx": counterpart,
                    "shared_inputs": r["shared_count"],
                    "delta_ms": r["delta_ms"] or 0.0,
                    "outcome": r["outcome"],
                    "counterpart_fee": r["tx_b_fee"] if is_a else r["tx_a_fee"],
                    "counterpart_ttl": counterpart_ttl or 0,
                    "shares_change_address": my_addr == counterpart_addr and my_addr != "",
                    "attacker_win_count": win_counts.get(counterpart_addr, 0),
                    "attacker_win_count_24h": win_counts_24h.get(counterpart_addr, 0),
                    "tx_role": "TX_A" if is_a else "TX_B",
                }
    return result


async def update_collision_outcome(confirmed_tx_hash: str, network: str):
    """Update collision outcomes when a tx is confirmed on-chain.

    Sets outcome based on which side of the collision confirmed:
    - TX_A_CONFIRMED if the confirmed tx is tx_a
    - TX_B_CONFIRMED if the confirmed tx is tx_b
    """
    async with get_connection() as conn:
        await conn.execute("""
            UPDATE mempool_collisions
            SET outcome = CASE
                WHEN tx_a = $1 THEN 'TX_A_CONFIRMED'
                WHEN tx_b = $1 THEN 'TX_B_CONFIRMED'
            END
            WHERE network = $2
              AND (tx_a = $1 OR tx_b = $1)
              AND outcome = 'BOTH_PENDING'
        """, confirmed_tx_hash, network)
