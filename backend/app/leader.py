"""Single-instance leader guard for ingestion + analysis.

The Ogmios chain-sync checkpoint (``save_sync_point``) and the analysis
engine's poll watermark both assume exactly one process is advancing them: a
second live instance reading the same Postgres/ClickHouse would double-insert
transactions and race the checkpoint update, corrupting the position both
readers rely on (review finding). This module gates "am I the leader" behind
a Postgres session-level advisory lock.

An advisory lock is scoped to the PG *session* that took it, not to a
transaction or a request, so it cannot be acquired through the shared
``asyncpg`` pool (a pooled connection is returned and reused by unrelated
callers between requests, which would silently drop the lock). Instead a
single connection is opened here, dedicated to holding the lock for the
process's whole lifetime, and closed only on release/shutdown.

For the same reason this requires a DIRECT Postgres connection: a
transaction-pooling proxy (e.g. PgBouncer in transaction mode) reassigns the
server session between statements, which silently breaks session-level
advisory locks. Point POSTGRES_HOST at the real server, or use a
session-mode pool, for the guard to hold.
"""

import logging
from typing import Optional

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

# The dedicated connection holding the lock, or None if this process is not
# (yet) the leader. Never drawn from app.db.postgres's pool — see module
# docstring.
_conn: Optional[asyncpg.Connection] = None


async def try_acquire() -> bool:
    """Attempt to become leader now (non-blocking). Returns True if this
    process holds the lock afterwards (including if it already did).

    On failure to acquire, the probe connection is closed immediately so a
    standby instance polling this repeatedly does not leak one connection
    per attempt.
    """
    global _conn
    if _conn is not None:
        return True
    probe = await asyncpg.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        database=settings.POSTGRES_DB,
        # Bound the probe below the standby retry cadence so a black-holing
        # Postgres cannot stack overlapping connection attempts.
        timeout=settings.LEADER_LOCK_RETRY_SECONDS,
    )
    try:
        acquired = await probe.fetchval(
            "SELECT pg_try_advisory_lock($1)", settings.LEADER_LOCK_KEY
        )
    except Exception:
        await probe.close()
        raise
    if not acquired:
        await probe.close()
        return False
    _conn = probe
    logger.info(
        "Leader lock acquired (key=%s) — this instance runs ingestion + analysis",
        settings.LEADER_LOCK_KEY,
    )
    return True


async def release() -> None:
    """Release the lock and close the dedicated connection, if held. Safe to
    call even if this process never became leader."""
    global _conn
    if _conn is None:
        return
    conn, _conn = _conn, None
    try:
        await conn.execute("SELECT pg_advisory_unlock($1)", settings.LEADER_LOCK_KEY)
    except Exception:
        pass  # process is shutting down either way; closing the session below
              # releases any session-level lock regardless.
    finally:
        await conn.close()


def is_leader() -> bool:
    """True if this process currently holds the leader lock."""
    return _conn is not None
