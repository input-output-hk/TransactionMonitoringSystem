"""spend_edges: the circular BFS's transfer graph (schema in clickhouse_schema).

One row per (address a confirmed transaction spends from, the transaction,
address it pays) with what it paid there, so a BFS hop is one primary-key range
read per frontier address (see graph._HOP_QUERY). Two writers fill it: the block
writer, for every block it inserts (rows_for), and a one-time chunked backfill
of the history ingested before the table existed (ensure_backfilled), which the
scoring lanes wait on so that no transaction is scored against a partial graph.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
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


async def ensure_backfilled(network: str, should_stop: Callable[[], bool]) -> None:
    """Fill the history once, resuming where a restart left it; return when complete.

    Runs on the backlog lane's executor, whose queries are thread-capped and
    which has nothing else to do until this returns. A failed chunk is retried
    every ANALYSIS_ENGINE_INTERVAL_SECONDS: the lanes must not score before the
    graph is whole, so there is nothing to give up to.
    """
    from app.db import clickhouse, postgres

    loop = asyncio.get_running_loop()
    while True:
        try:
            next_chunk, completed = await postgres.get_spend_edges_backfill(network)
            if completed:
                return
            if next_chunk == 0:
                logger.info(
                    "spend_edges: filling the history in %d chunks before scoring starts",
                    BACKFILL_CHUNKS,
                )
            for chunk in range(next_chunk, BACKFILL_CHUNKS):
                if should_stop():
                    return
                await loop.run_in_executor(
                    clickhouse._ch_backlog_executor, backfill_chunk, network, chunk
                )
                done = chunk + 1 == BACKFILL_CHUNKS
                await postgres.save_spend_edges_backfill(network, chunk + 1, done)
            logger.info("spend_edges: history filled")
            return
        except Exception as e:
            logger.error(
                "spend_edges backfill failed, retrying in %ss: %s",
                settings.ANALYSIS_ENGINE_INTERVAL_SECONDS,
                e,
            )
            await asyncio.sleep(settings.ANALYSIS_ENGINE_INTERVAL_SECONDS)
