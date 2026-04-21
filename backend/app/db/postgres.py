"""PostgreSQL database connection and operations"""

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


async def init_pool():
    """Initialize PostgreSQL connection pool"""
    global _pool
    if _pool is None:
        try:
            _pool = await asyncpg.create_pool(
                host=settings.POSTGRES_HOST,
                port=settings.POSTGRES_PORT,
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD,
                database=settings.POSTGRES_DB,
                min_size=2,
                max_size=10,
                # Recycle connections idle > 5 min so a PG restart or transient
                # network blip doesn't leave stale sockets in the pool.
                max_inactive_connection_lifetime=300.0,
                # Cap any single statement at 30 s to prevent a stuck query from
                # pinning a pool slot indefinitely.
                command_timeout=30.0,
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
        """)

        logger.info("PostgreSQL schema initialized")


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
        # asyncpg returns "UPDATE N" — extract the row count
        return int(result.split()[1])


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
    if counterpart_addrs:
        async with get_connection() as conn:
            win_rows = await conn.fetch("""
                SELECT addr, COUNT(*) AS cnt FROM (
                    SELECT tx_a_first_input_addr AS addr
                    FROM mempool_collisions
                    WHERE network = $1 AND outcome = 'TX_A_CONFIRMED'
                      AND tx_a_first_input_addr = ANY($2)
                    UNION ALL
                    SELECT tx_b_first_input_addr AS addr
                    FROM mempool_collisions
                    WHERE network = $1 AND outcome = 'TX_B_CONFIRMED'
                      AND tx_b_first_input_addr = ANY($2)
                ) sub GROUP BY addr
            """, network, list(counterpart_addrs))
            win_counts = {r["addr"]: r["cnt"] for r in win_rows}

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


async def count_collision_wins(address: str, network: str) -> int:
    """Count how many collisions an address cluster has won (for recurrence scoring)."""
    async with get_connection() as conn:
        row = await conn.fetchrow("""
            SELECT COUNT(*) AS cnt
            FROM mempool_collisions
            WHERE network = $1
              AND (
                  (outcome = 'TX_A_CONFIRMED' AND tx_a_first_input_addr = $2)
                  OR (outcome = 'TX_B_CONFIRMED' AND tx_b_first_input_addr = $2)
              )
        """, network, address)
        return row["cnt"] if row else 0
