"""spend_edges: the circular BFS's transfer graph (schema in clickhouse_schema).

One row per (address a confirmed transaction spends from, the transaction,
address it pays) with what it paid there, so a BFS hop is one primary-key range
read per frontier address (see graph._HOP_QUERY). Two writers fill it: the block
writer, for every block it inserts (rows_for), and a one-time chunked backfill
of the history ingested before the table existed (ensure_backfilled), which the
scoring lanes wait on so that no transaction is scored against a partial graph.
Every start also checks the stored history for chunks whose edges have gone
since (missing_chunks) and fills those again before the lanes score.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.models.transaction import NormalizedTransaction

logger = logging.getLogger(__name__)

# What a hop follows, and so what an edge is made of. Shared by the block
# writer and the backfill so the two cannot drift; graph._HOP_QUERY reads them
# back without conditions of its own.
#
# A spend: a regular input. Collateral, reference inputs and the inputs of a
# phase-2-failed transaction move no value from their address. An input whose
# address was never resolved is empty, and no hop can follow it.
_SPEND_INPUT_CONDITIONS = (
    "is_collateral = 0 AND is_reference = 0 AND is_unspent_attempt = 0 AND address != ''"
)
# A payment: a regular output. A collateral return is paid only when the
# transaction fails, and then by the collateral, not by a spend.
_PAID_OUTPUT_CONDITIONS = "is_collateral = 0"

EDGE_COLUMNS = (
    "network",
    "address",
    "slot",
    "tx_hash",
    "to_address",
    "paid",
    "ingestion_timestamp",
)
_INSERT = f"INSERT INTO spend_edges ({', '.join(EDGE_COLUMNS)}) VALUES"

# Transaction hashes are lowercase hex, so their first two digits split the
# history into this many ranges of the (network, tx_hash) primary keys the three
# source tables share: each chunk is a range read of all three, a few thousand
# transactions on mainnet, small enough to join in memory under any limit.
_HEX_DIGITS_PER_CHUNK = 2
BACKFILL_CHUNKS = 16**_HEX_DIGITS_PER_CHUNK
# Sorts after every hex digit, closing the last chunk's range.
_PAST_LAST_HEX = "g"


def rows_for(transactions: list[NormalizedTransaction], now: datetime) -> list[tuple[Any, ...]]:
    """The spend_edges rows of a block's transactions, for the block writer.

    A slotless (mempool) copy has none: no hop window holds it, and the
    confirming block writes its edges with the slot.
    """
    rows: list[tuple[Any, ...]] = []
    for tx in transactions:
        if tx.slot is None:
            continue
        spenders = sorted(
            {
                inp.address
                for inp in tx.inputs
                if inp.address
                and not (inp.is_collateral or inp.is_reference or inp.is_unspent_attempt)
            }
        )
        if not spenders:
            continue
        paid: dict[str, int] = {}
        for out in tx.outputs:
            if out.is_collateral:
                continue
            paid[out.address] = paid.get(out.address, 0) + out.amount
        network = tx.network or settings.CARDANO_NETWORK
        stamp = tx.ingestion_timestamp or now
        rows.extend(
            (network, spender, tx.slot, tx.tx_hash, to_address, amount, stamp)
            for spender in spenders
            for to_address, amount in sorted(paid.items())
        )
    return rows


def insert_rows(client: Any, rows: list[tuple[Any, ...]]) -> None:
    if rows:
        client.execute(_INSERT, rows)


# One chunk of the history, read from the three source tables the way the hop
# query read them before this table existed: every output counted once
# (DISTINCT drops a replayed block's unmerged duplicates) and summed per
# address, every slot the transaction is stored at (a slotless mempool copy has
# none), stamped with the latest ingestion of that copy. Re-running a chunk
# writes the same keys, which the ReplacingMergeTree collapses.
#
# A rollback purge landing between a chunk's read and its write leaves that
# chunk's edges of the orphaned transaction behind: the DELETE applies only to
# the parts that existed when it ran. Accepted, since it can happen only while
# the one-time fill runs and the stray edge is a leg that was on chain, so at
# worst a cycle is reported that the fork undid, never one missed.
_BACKFILL_CHUNK = f"""
    INSERT INTO spend_edges ({", ".join(EDGE_COLUMNS)})
    SELECT s.network, s.address, t.tx_slot, s.tx_hash, o.to_address, o.paid, t.stamp
    FROM (
        SELECT DISTINCT network, tx_hash, address
        FROM transaction_inputs
        WHERE network = %(network)s AND tx_hash >= %(lo)s AND tx_hash < %(hi)s
          AND {_SPEND_INPUT_CONDITIONS}
    ) AS s
    INNER JOIN (
        SELECT tx_hash, address AS to_address, sum(amount) AS paid
        FROM (
            SELECT DISTINCT tx_hash, output_index, address, amount
            FROM transaction_outputs
            WHERE network = %(network)s AND tx_hash >= %(lo)s AND tx_hash < %(hi)s
              AND {_PAID_OUTPUT_CONDITIONS}
        )
        GROUP BY tx_hash, address
    ) AS o ON o.tx_hash = s.tx_hash
    INNER JOIN (
        SELECT tx_hash, assumeNotNull(slot) AS tx_slot, max(ingestion_timestamp) AS stamp
        FROM transactions
        WHERE network = %(network)s AND tx_hash >= %(lo)s AND tx_hash < %(hi)s
          AND isNotNull(slot)
        GROUP BY tx_hash, slot
    ) AS t ON t.tx_hash = s.tx_hash
    SETTINGS optimize_use_projections = 0
"""


def _chunk_bounds(chunk: int) -> tuple[str, str]:
    lo = format(chunk, f"0{_HEX_DIGITS_PER_CHUNK}x")
    hi = (
        format(chunk + 1, f"0{_HEX_DIGITS_PER_CHUNK}x")
        if chunk + 1 < BACKFILL_CHUNKS
        else _PAST_LAST_HEX
    )
    return lo, hi


def backfill_chunk(network: str, chunk: int) -> None:
    """Fill one chunk of the history (blocking; runs on the backlog executor)."""
    from app.db import clickhouse

    lo, hi = _chunk_bounds(chunk)
    clickhouse._get_client().execute(_BACKFILL_CHUNK, {"network": network, "lo": lo, "hi": hi})


# The spends stored before `before` that the fill would give edges and that
# have none, by chunk. The progress row lives in Postgres and the edges in
# ClickHouse, which are restored, dropped and repaired apart, so the row can
# read "completed", or resume at a later chunk, over a table with any chunk
# missing: a ClickHouse snapshot taken partway through the fill, a backup taken
# during it (scripts/backup.sh does not export spend_edges), a broken part
# detached after an unclean shutdown (the table is ordered by address, so one
# part holds edges of every chunk). Every spend stored before the process
# started has its edges by then: the writer makes them in the same attempt,
# the fill made the rest.
#
# Per transaction, never a count per chunk: the block writer adds edges from
# the moment this process starts, and they would mask missing old ones. The
# spend side takes _BACKFILL_CHUNK's joins (a slotted copy, a paid output), so
# a dropped mempool transaction or one paying only a collateral return is
# never a gap. The edge side has no time bound: a transaction seen in the
# mempool before `before` and confirmed after it has its edges stamped after.
# Spends near their retention are left out (SPEND_EDGES_CHECK_TTL_MARGIN_HOURS).
# Projections off as in _BACKFILL_CHUNK: p_by_time_v2 can still serve a copy a
# rollback purge deleted (see clickhouse.delete_score_rows), a gap that no
# refill could close.
_MISSING_EDGES = f"""
    SELECT DISTINCT substring(tx_hash, 1, {_HEX_DIGITS_PER_CHUNK})
    FROM transaction_inputs
    WHERE network = %(network)s AND tx_hash >= %(lo)s AND tx_hash < %(hi)s
      AND {_SPEND_INPUT_CONDITIONS}
      AND ingestion_timestamp < %(before)s AND ingestion_timestamp >= %(expiring_before)s
      AND tx_hash IN (
          SELECT tx_hash FROM transactions
          WHERE network = %(network)s AND tx_hash >= %(lo)s AND tx_hash < %(hi)s
            AND isNotNull(slot)
      )
      AND tx_hash IN (
          SELECT tx_hash FROM transaction_outputs
          WHERE network = %(network)s AND tx_hash >= %(lo)s AND tx_hash < %(hi)s
            AND {_PAID_OUTPUT_CONDITIONS}
      )
      AND tx_hash NOT IN (
          SELECT tx_hash FROM spend_edges
          WHERE network = %(network)s AND tx_hash >= %(lo)s AND tx_hash < %(hi)s
      )
    SETTINGS optimize_use_projections = 0
"""
# DateTime's own minimum: with no retention set, no spend is left out.
_NO_EXPIRY = datetime.fromtimestamp(0, UTC)


def missing_chunks(network: str, before: datetime) -> list[int]:
    """The chunks with a spend stored before `before` but no edges for it (blocking).

    Runs on the backlog executor, one query per SPEND_EDGES_CHECK_CHUNKS_PER_PASS
    chunks, so that no pass holds more of the history in memory than that.
    """
    from app.db import clickhouse

    client = clickhouse._get_client()
    expiring_before = _NO_EXPIRY
    if settings.CH_RETENTION_DAYS_IO > 0:
        expiring_before = (
            datetime.now(UTC)
            - timedelta(days=settings.CH_RETENTION_DAYS_IO)
            + timedelta(hours=settings.SPEND_EDGES_CHECK_TTL_MARGIN_HOURS)
        )
    per_pass = settings.SPEND_EDGES_CHECK_CHUNKS_PER_PASS
    missing: list[int] = []
    for first in range(0, BACKFILL_CHUNKS, per_pass):
        lo, _ = _chunk_bounds(first)
        _, hi = _chunk_bounds(min(first + per_pass, BACKFILL_CHUNKS) - 1)
        rows = client.execute(
            _MISSING_EDGES,
            {
                "network": network,
                "lo": lo,
                "hi": hi,
                "before": before,
                "expiring_before": expiring_before,
            },
        )
        missing.extend(int(prefix, 16) for (prefix,) in rows)
    return sorted(missing)


async def _refill_missing(
    network: str,
    before: datetime,
    should_stop: Callable[[], bool],
    on_progress: Callable[[int], None] | None,
) -> int | None:
    """Fill again the chunks missing_chunks finds, then check once more.

    Returns how many are still missing, or None when stopped first. A gap the
    refill does not close is logged and scoring starts anyway: holding it until
    the graph is whole would stop all detection, which misses more than the gap.
    """
    from app.db import clickhouse

    loop = asyncio.get_running_loop()
    missing = await loop.run_in_executor(
        clickhouse._ch_backlog_executor, missing_chunks, network, before
    )
    if on_progress is not None:
        on_progress(BACKFILL_CHUNKS - len(missing))
    if not missing:
        return 0
    logger.error(
        "spend_edges: chunks %s have spends stored before this start without their edges "
        "(ClickHouse restored apart from Postgres, or parts lost); filling them again "
        "before scoring starts",
        missing,
    )
    for refilled, chunk in enumerate(missing, 1):
        if should_stop():
            return None
        await loop.run_in_executor(clickhouse._ch_backlog_executor, backfill_chunk, network, chunk)
        if on_progress is not None:
            on_progress(BACKFILL_CHUNKS - len(missing) + refilled)
    still_missing = await loop.run_in_executor(
        clickhouse._ch_backlog_executor, missing_chunks, network, before
    )
    if still_missing:
        logger.error(
            "spend_edges: chunks %s still have spends without their edges after a refill; "
            "scoring starts anyway, and cycles through them can be missed",
            still_missing,
        )
    else:
        logger.info("spend_edges: missing chunks filled again")
    return len(still_missing)


async def ensure_backfilled(
    network: str,
    should_stop: Callable[[], bool],
    history_before: datetime | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int | None:
    """Fill the history once, resuming where a restart left it; return when complete.

    Runs on the backlog lane's executor, whose queries are thread-capped and
    which has nothing else to do until this returns. A failed chunk is retried
    every ANALYSIS_ENGINE_INTERVAL_SECONDS: the lanes must not score before the
    graph is whole, so there is nothing to give up to. on_progress gets the
    count of chunks known filled after each one, so a fill or refill that stops
    advancing shows as a stall (analysis.scoring_status).

    history_before is when this process started. A fill recorded as complete,
    and one that has just completed (it may have resumed over a ClickHouse
    restored apart from its progress row), is checked against the spends stored
    before it, and the chunks found missing are filled again (missing_chunks).
    Returns how many chunks are still missing after that, or None when the
    check did not run: history_before None, a stop first, or a check that failed
    SPEND_EDGES_CHECK_ATTEMPTS times. The fill is retried for as long as it
    takes, since the history it writes is not there at all until it ends; the
    check is not, since the history it checks is complete as far as its record
    goes, and a check failing every time would otherwise hold both lanes for good.
    """
    from app.db import clickhouse, postgres

    loop = asyncio.get_running_loop()
    while True:
        try:
            next_chunk, completed = await postgres.get_spend_edges_backfill(network)
            if not completed:
                if next_chunk == 0:
                    logger.info(
                        "spend_edges: filling the history in %d chunks before scoring starts",
                        BACKFILL_CHUNKS,
                    )
                for chunk in range(next_chunk, BACKFILL_CHUNKS):
                    if should_stop():
                        return None
                    await loop.run_in_executor(
                        clickhouse._ch_backlog_executor, backfill_chunk, network, chunk
                    )
                    done = chunk + 1 == BACKFILL_CHUNKS
                    await postgres.save_spend_edges_backfill(network, chunk + 1, done)
                    if on_progress is not None:
                        on_progress(chunk + 1)
                logger.info("spend_edges: history filled")
            break
        except Exception as e:
            logger.error(
                "spend_edges backfill failed, retrying in %ss: %s",
                settings.ANALYSIS_ENGINE_INTERVAL_SECONDS,
                e,
            )
            await asyncio.sleep(settings.ANALYSIS_ENGINE_INTERVAL_SECONDS)
    if history_before is None:
        return None
    attempts = settings.SPEND_EDGES_CHECK_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            return await _refill_missing(network, history_before, should_stop, on_progress)
        except Exception as e:
            if attempt == attempts:
                logger.error(
                    "spend_edges: the coverage check failed %d times; scoring starts "
                    "without it, and cycles through a chunk it would have refilled can be "
                    "missed: %s",
                    attempts,
                    e,
                )
                return None
            logger.error(
                "spend_edges: the coverage check failed, retrying in %ss: %s",
                settings.ANALYSIS_ENGINE_INTERVAL_SECONDS,
                e,
            )
            await asyncio.sleep(settings.ANALYSIS_ENGINE_INTERVAL_SECONDS)
    return None
