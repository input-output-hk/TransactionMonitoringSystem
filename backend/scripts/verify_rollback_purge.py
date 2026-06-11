#!/usr/bin/env python3
"""Live verification that the rollback purge works on a real ClickHouse.

WHY: lightweight DELETE on a table with projections throws on ClickHouse
>= 24.7 unless lightweight_mutation_projection_mode is set, and every
persistence test mocks the client, so only a real server can prove the
purge path survives. Run this once against the deployed server version
after any change to delete_rolled_back_txs or the transactions projection.

Usage:  cd backend && python scripts/verify_rollback_purge.py
        (requires a reachable ClickHouse, e.g. docker compose up -d clickhouse)

Uses a sentinel network so real data is never touched. Exits non-zero on
any failure.
"""

import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import clickhouse  # noqa: E402
from app.models.transaction import NormalizedTransaction  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("verify_rollback_purge")

# Sentinel network: rows under it are created and deleted by this script only.
_VERIFY_NETWORK = "verify_purge"
_VERIFY_TX_HASH = "ff" * 32
_VERIFY_SLOT = 100
_ROLLBACK_SLOT = 50  # below _VERIFY_SLOT so the purge must select the row


def main() -> int:
    clickhouse.init_client()
    client = clickhouse._get_client()

    logger.info("Applying schema (runs the projection migration)...")
    clickhouse.execute_schema()

    ddl = client.execute(
        "SELECT create_table_query FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'transactions'"
    )[0][0]
    if "p_by_time_v2" not in ddl:
        logger.error("FAIL: p_by_time_v2 projection missing from transactions DDL")
        return 1
    if "PROJECTION p_by_time " in ddl or "p_by_time (" in ddl.replace("p_by_time_v2", ""):
        logger.error("FAIL: legacy p_by_time projection still present")
        return 1
    logger.info("OK: projection is p_by_time_v2, legacy p_by_time absent")

    # Clean any leftovers from a prior failed run, then insert the synthetic tx.
    clickhouse.delete_rolled_back_txs(_VERIFY_NETWORK, 0)
    tx = NormalizedTransaction(
        tx_hash=_VERIFY_TX_HASH,
        network=_VERIFY_NETWORK,
        slot=_VERIFY_SLOT,
        block_height=1,
        block_hash="aa" * 32,
        timestamp=datetime.now(timezone.utc),
        fee=0,
        raw_data={},
    )
    clickhouse.insert_transactions_batch([tx])
    logger.info("Inserted synthetic tx under network=%s", _VERIFY_NETWORK)

    try:
        purged = clickhouse.delete_rolled_back_txs(_VERIFY_NETWORK, _ROLLBACK_SLOT)
    except Exception:
        logger.exception(
            "FAIL: rollback purge raised on a real server — the crash-loop "
            "bug is NOT fixed"
        )
        return 1
    if len(purged) != 1:
        logger.error("FAIL: expected 1 purged tx, got %s", purged)
        return 1
    # The delayed second pass must run clean on the real server too.
    clickhouse.delete_score_rows(_VERIFY_NETWORK, purged)

    remaining = client.execute(
        "SELECT count() FROM transactions WHERE network = %(n)s",
        {"n": _VERIFY_NETWORK},
    )[0][0]
    if remaining != 0:
        logger.error("FAIL: %d rows survived the purge", remaining)
        return 1

    logger.info("OK: purge deleted the row without raising. All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
