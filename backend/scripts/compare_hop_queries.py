#!/usr/bin/env python3
"""Replay real circular BFS hops on the spend_edges query and the one it replaced.

WHY: graph._HOP_QUERY moved from address_transactions + transaction_inputs +
transactions + transaction_outputs (each confirmed by tx_hash IN) to one read of
spend_edges. The circular detector's recall rests on the two returning the same
rows, and only real data has the shapes a planted test lacks. For each sampled
origin this runs graph.detect_cycle and, at every hop, both queries with the
same parameters, in alternating order; any difference in the rows is reported
and fails the run. Their CPU, bytes and memory come from system.query_log.

Usage (from backend/, against the deployment's ClickHouse):
    python scripts/compare_hop_queries.py --fill            # fill spend_edges first
    python scripts/compare_hop_queries.py --since 2026-10-08T00:00:00 \\
        --until 2026-10-09T00:00:00 --origins 40

--fill creates spend_edges if missing and runs every backfill chunk WITHOUT
recording progress in Postgres, so the application's own backfill still runs
in full when it starts (re-filled rows collapse in the ReplacingMergeTree).
Origins must have a whole window behind them (until + 24h before the fill),
or edges ingested after the fill are missing and differences are expected.
Exits non-zero on any difference.
"""

import argparse
import logging
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analysis import graph
from app.config import settings
from app.db import clickhouse, spend_edges
from app.db.clickhouse_schema import SCHEMA_DDL
from app.logging_utils import setup_logging

setup_logging()
logger = logging.getLogger("compare_hop_queries")

# The hop query before spend_edges (6919bb3), kept here only to compare against.
_LEGACY_PREFILTER = """
    SELECT tx_hash
    FROM address_transactions
    WHERE network = %(network)s
      AND address IN %(addresses)s
      AND slot >= %(min_slot)s
      AND slot <= %(max_slot)s
"""
_LEGACY_SPENDS = f"""
    SELECT tx_hash
    FROM transaction_inputs
    WHERE network = %(network)s
      AND address IN %(addresses)s
      AND is_collateral = 0
      AND is_reference = 0
      AND is_unspent_attempt = 0
      AND tx_hash != %(origin_tx)s
      AND tx_hash IN ({_LEGACY_PREFILTER})
"""
LEGACY_HOP_QUERY = f"""
    WITH cand AS (
        SELECT DISTINCT tx_hash, slot
        FROM transactions
        WHERE network = %(network)s
          AND slot >= %(min_slot)s
          AND slot <= %(max_slot)s
          AND tx_hash IN ({_LEGACY_SPENDS})
    )
    SELECT c.tx_hash, to2.address, sum(to2.amount) AS paid, c.slot
    FROM cand c
    JOIN (
        SELECT DISTINCT tx_hash, output_index, address, amount
        FROM transaction_outputs
        WHERE network = %(network)s
          AND is_collateral = 0
          AND tx_hash IN (SELECT tx_hash FROM cand)
    ) to2 ON c.tx_hash = to2.tx_hash
    GROUP BY c.tx_hash, c.slot, to2.address
    ORDER BY c.slot ASC, c.tx_hash ASC, to2.address ASC
    LIMIT %(hop_row_limit)s
"""

# Cores and memory the replay may take on a host that keeps ingesting and scoring.
_REPLAY_MAX_THREADS = 2
_REPLAY_MAX_MEMORY_BYTES = 1_000_000_000


class _Comparing:
    """Client stand-in: runs both hop queries for every hop, passes the rest on."""

    def __init__(self, client, run_id):
        self._client = client
        self._run_id = run_id
        self.hops = 0
        self.differences: list[dict] = []

    def _run(self, sql, params, variant):
        return self._client.execute(
            sql,
            params,
            settings={
                "max_threads": _REPLAY_MAX_THREADS,
                "max_memory_usage": _REPLAY_MAX_MEMORY_BYTES,
                "log_comment": f"{self._run_id}:{variant}",
            },
        )

    def execute(self, sql, params=None, **kwargs):
        if sql != graph._HOP_QUERY:
            return self._client.execute(sql, params, **kwargs)
        order = ("edges", "legacy") if self.hops % 2 == 0 else ("legacy", "edges")
        rows = {
            variant: [
                tuple(r)
                for r in self._run(
                    graph._HOP_QUERY if variant == "edges" else LEGACY_HOP_QUERY, params, variant
                )
            ]
            for variant in order
        }
        self.hops += 1
        if rows["edges"] != rows["legacy"]:
            self.differences.append(
                {
                    "origin": params["origin_tx"],
                    "frontier": len(params["addresses"]),
                    "edges_only": sorted(set(rows["edges"]) - set(rows["legacy"]))[:5],
                    "legacy_only": sorted(set(rows["legacy"]) - set(rows["edges"]))[:5],
                    "rows": (len(rows["edges"]), len(rows["legacy"])),
                }
            )
        return rows["edges"]


def _fill(network: str) -> None:
    client = clickhouse._get_client()
    client.execute(SCHEMA_DDL["spend_edges"].format(table="spend_edges"))
    for chunk in range(spend_edges.BACKFILL_CHUNKS):
        spend_edges.backfill_chunk(network, chunk)
    [(rows,)] = client.execute(
        "SELECT count() FROM spend_edges WHERE network = %(network)s", {"network": network}
    )
    logger.info("spend_edges filled: %d rows (progress not recorded)", rows)


def _origins(network: str, since: datetime, until: datetime, count: int) -> list[str]:
    rows = clickhouse._get_client().execute(
        """
        SELECT tx_hash FROM transactions
        WHERE network = %(network)s AND timestamp >= %(since)s AND timestamp < %(until)s
          AND isNotNull(slot)
        GROUP BY tx_hash
        ORDER BY cityHash64(tx_hash)
        LIMIT %(count)s
        """,
        {"network": network, "since": since, "until": until, "count": count},
    )
    return [r[0] for r in rows]


def _costs(run_id: str) -> None:
    client = clickhouse._get_client()
    client.execute("SYSTEM FLUSH LOGS")
    rows = client.execute(
        """
        SELECT splitByChar(':', log_comment)[2] AS variant, count(),
               round(sum(ProfileEvents['OSCPUVirtualTimeMicroseconds']) / 1e6, 1),
               formatReadableSize(sum(read_bytes)),
               formatReadableSize(quantile(0.95)(memory_usage)),
               formatReadableSize(max(memory_usage))
        FROM system.query_log
        WHERE type = 'QueryFinish' AND startsWith(log_comment, %(run)s)
          AND event_date >= yesterday()
        GROUP BY variant ORDER BY variant
        """,
        {"run": f"{run_id}:"},
    )
    for variant, n, cpu, read, mem_p95, mem_max in rows:
        logger.info(
            "%s: %d hops, %s CPU-s, %s read, memory p95 %s max %s",
            variant,
            n,
            cpu,
            read,
            mem_p95,
            mem_max,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--network", default=settings.CARDANO_NETWORK)
    parser.add_argument("--fill", action="store_true")
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument("--until", type=datetime.fromisoformat)
    parser.add_argument("--origins", type=int, default=0)
    args = parser.parse_args()

    clickhouse.init_client()
    if args.fill:
        _fill(args.network)
    if not args.origins:
        return 0
    if args.since is None or args.until is None:
        parser.error("--origins needs --since and --until")

    run_id = f"hopcmp-{uuid.uuid4().hex[:8]}"
    comparing = _Comparing(clickhouse._get_client(), run_id)
    real_get_client = clickhouse._get_client
    clickhouse._get_client = lambda: comparing  # type: ignore[assignment]
    cycles = 0
    try:
        for origin in _origins(args.network, args.since, args.until, args.origins):
            if graph.detect_cycle(origin, args.network) is not None:
                cycles += 1
    finally:
        clickhouse._get_client = real_get_client  # type: ignore[assignment]

    logger.info(
        "%s: %d origins, %d hops compared, %d cycles, %d differing hops",
        run_id,
        args.origins,
        comparing.hops,
        cycles,
        len(comparing.differences),
    )
    for difference in comparing.differences:
        logger.error("hop differs: %r", difference)
    _costs(run_id)
    return 1 if comparing.differences else 0


if __name__ == "__main__":
    sys.exit(main())
