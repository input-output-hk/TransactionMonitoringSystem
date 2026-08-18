"""Backfill ``tx_class_scores.contract_address`` from the stored evidence blob.

The column is additive (``ADD COLUMN IF NOT EXISTS ... DEFAULT ''``), so every
row analysed before it existed reads as "no contract identity". That is not
wrong, but it makes the contract-grouped alerts view empty for all history. The
value is already recoverable: each scorer wrote the contract it implicated into
``evidence`` under its own key, and :mod:`app.analysis.contract_identity` owns
the mapping from winning class to key.

The rewrite is a ClickHouse mutation rather than a re-score: the scoring inputs
are untouched, so re-running the engine would risk moving scores under a config
that has since been recalibrated, for a column that is a pure projection of data
already on the row.

Work is chunked by ``analyzed_at`` window and every chunk is guarded on
``contract_address = ''``, so the script is idempotent, resumable, and safe to
re-run after an interruption. It never overwrites a value already present.

Run with ``--apply`` to write; default is dry-run.

  python -m scripts.oneoff.backfill_contract_address --network mainnet
  python -m scripts.oneoff.backfill_contract_address --network mainnet --apply

``--chunk-days`` trades mutation size against mutation count; the default keeps
each mutation over a bounded slice of history rather than the whole table.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta

from app.analysis.contract_identity import CONTRACT_EVIDENCE_KEYS, NO_CONTRACT
from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

# Default mutation window. One ClickHouse mutation rewrites every part it
# touches, so a whole-table UPDATE on mainnet history would rewrite the entire
# table in one unbounded operation. 30 days keeps each step observable and
# interruptible without producing thousands of mutations.
DEFAULT_CHUNK_DAYS = 30

# Oldest data to consider when the caller does not bound the range. The table
# starts far later than this, and the loop stops as soon as it runs out of rows,
# so this is only a floor for the initial scan.
DEFAULT_LOOKBACK_DAYS = 3650


def _case_expression() -> str:
    """SQL mapping the winning class to its evidence key, as a CASE.

    Built from CONTRACT_EVIDENCE_KEYS so the SQL cannot drift from the Python
    the write path uses. Classes absent from the map have no contract identity
    and fall through to ''.

    The contract_anomaly branch is inert here: that class is a read-time overlay
    and is never a STORED max_class. It is emitted anyway rather than special-
    cased out, so this stays a faithful projection of the one mapping instead of
    a second place that has to know which classes reach the table.
    """
    branches = []
    for attack_class, key in sorted(CONTRACT_EVIDENCE_KEYS.items()):
        branches.append(
            f"WHEN max_class = '{attack_class}' "
            f"THEN JSONExtractString(evidence, '{attack_class}', '{key}')"
        )
    return "CASE " + " ".join(branches) + " ELSE '' END"


def _count_pending(client, network: str, start: datetime, end: datetime) -> int:
    rows = client.execute(
        """
        SELECT count() FROM tx_class_scores FINAL
        WHERE network = %(network)s
          AND contract_address = %(empty)s
          AND analyzed_at >= %(start)s AND analyzed_at < %(end)s
        """,
        {"network": network, "empty": NO_CONTRACT, "start": start, "end": end},
    )
    return int(rows[0][0]) if rows else 0


def _preview(client, network: str, start: datetime, end: datetime, limit: int) -> list[tuple]:
    """Sample of (max_class, derived address) a chunk would write."""
    return client.execute(
        f"""
        SELECT max_class, {_case_expression()} AS derived, count() AS n
        FROM tx_class_scores FINAL
        WHERE network = %(network)s
          AND contract_address = %(empty)s
          AND analyzed_at >= %(start)s AND analyzed_at < %(end)s
        GROUP BY max_class, derived
        ORDER BY n DESC
        LIMIT %(limit)s
        """,
        {
            "network": network,
            "empty": NO_CONTRACT,
            "start": start,
            "end": end,
            "limit": limit,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--network", default=settings.CARDANO_NETWORK)
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=DEFAULT_CHUNK_DAYS,
        help=f"Days per mutation (default: {DEFAULT_CHUNK_DAYS})",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="How far back to scan (default: effectively all history)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the mutations. Without it the script only reports.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help=(
            "Wait for each mutation to finish before starting the next "
            "(mutations_sync=2). Slower but keeps load predictable."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.chunk_days < 1:
        logger.error("--chunk-days must be at least 1")
        sys.exit(2)

    client = clickhouse._get_client()
    now = datetime.now()
    start = now - timedelta(days=args.lookback_days)
    total_pending = _count_pending(client, args.network, start, now)
    logger.info(
        "network=%s rows with no contract_address in window: %d",
        args.network,
        total_pending,
    )
    if not total_pending:
        logger.info("Nothing to backfill.")
        return

    case_sql = _case_expression()
    updated_chunks = 0
    window_start = start
    while window_start < now:
        window_end = min(window_start + timedelta(days=args.chunk_days), now)
        pending = _count_pending(client, args.network, window_start, window_end)
        if not pending:
            window_start = window_end
            continue
        logger.info(
            "chunk %s .. %s: %d rows",
            window_start.date(),
            window_end.date(),
            pending,
        )
        for max_class, derived, n in _preview(client, args.network, window_start, window_end, 5):
            logger.info(
                "    %-18s -> %-24s (%d rows)",
                max_class or "<none>",
                (derived[:20] + "...") if len(derived) > 23 else (derived or "<no identity>"),
                n,
            )
        if args.apply:
            settings_clause = " SETTINGS mutations_sync = 2" if args.sync else ""
            client.execute(
                f"""
                ALTER TABLE tx_class_scores
                UPDATE contract_address = {case_sql}
                WHERE network = %(network)s
                  AND contract_address = %(empty)s
                  AND analyzed_at >= %(start)s AND analyzed_at < %(end)s
                {settings_clause}
                """,
                {
                    "network": args.network,
                    "empty": NO_CONTRACT,
                    "start": window_start,
                    "end": window_end,
                },
            )
            updated_chunks += 1
        window_start = window_end

    if args.apply:
        logger.info("Submitted %d chunk mutation(s).", updated_chunks)
        if not args.sync:
            logger.info(
                "Mutations are asynchronous. Track them with: "
                "SELECT table, mutation_id, is_done FROM system.mutations "
                "WHERE table = 'tx_class_scores' AND is_done = 0"
            )
    else:
        logger.info("Dry run. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
