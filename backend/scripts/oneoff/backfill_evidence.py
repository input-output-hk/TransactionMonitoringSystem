"""Backfill ``evidence`` for analyzed transactions that predate the column.

Selects rows where ``evidence`` is empty (``'{}'`` from the
``ADD COLUMN IF NOT EXISTS`` default), re-runs the scoring engine, and
re-inserts them. tx_class_scores is a ReplacingMergeTree keyed on
``(network, tx_hash)`` deduped by ``max(analyzed_at)``, so the newer row
supersedes the old one in the same partition.

The scoring pipeline itself is deterministic given the same input rows
and config, so scores should not move; the only material delta is that
``evidence`` is now populated. The script reports any score / class
changes anyway so an operator can spot drift from config tweaks.

Run with ``--apply`` to write; default is dry-run.

  python -m scripts.oneoff.backfill_evidence --network preprod
  python -m scripts.oneoff.backfill_evidence --network preprod --apply

Use ``--limit`` to run a small slice end-to-end before doing the full pass.
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import timedelta

from clickhouse_driver import Client

from app.analysis.engine import (
    _build_scorers,
    _enrich_cycle_features,
    _enrich_inputs_with_resolved_addresses,
    _enrich_sandwich_features,
    _score_transaction,
)
from app.config import settings
from app.db import clickhouse, postgres

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--network",
        default=settings.CARDANO_NETWORK,
        help="Cardano network to backfill (default: configured network)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap rows processed (0 = no cap). Useful for a smoke run.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="Only consider rows analyzed within the last N days (0 = all).",
    )
    parser.add_argument(
        "--all-rows",
        action="store_true",
        help=(
            "Include scored-but-clean rows (no class hit). Default is to "
            "backfill only rows that triggered an attack class, since those "
            "are the only ones whose evidence ever appears in the UI."
        ),
    )
    parser.add_argument(
        "--min-band",
        choices=["Low", "Moderate", "High", "Critical"],
        default=None,
        help=(
            "Restrict to rows at or above this risk band. Useful when you "
            "only care to populate evidence for triage-worthy alerts. "
            "Examples: --min-band High covers High + Critical."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Insert re-scored rows. Default is dry-run.",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help=(
            "Print just the matching row count and exit. Skips the per-row "
            "enrichment + scoring passes, so it's ~instant. Use this to "
            "size up the work before committing to --apply."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-score rows even if evidence is already populated. Use after "
            "an evidence-shape fix to refresh existing rows that were "
            "backfilled with the old (buggy) values."
        ),
    )
    args = parser.parse_args()

    client = Client(
        host=settings.CLICKHOUSE_HOST,
        port=settings.CLICKHOUSE_PORT,
        user=settings.CLICKHOUSE_USER,
        password=settings.CLICKHOUSE_PASSWORD,
        database=settings.CLICKHOUSE_DB,
    )

    limit_clause = f"LIMIT {int(args.limit)}" if args.limit > 0 else ""
    days_clause = (
        f"AND s.analyzed_at >= now() - INTERVAL {int(args.days)} DAY"
        if args.days > 0
        else ""
    )
    # Default to alert-only rows: a tx with max_class='' / max_score=0 never
    # surfaces on the alerts list, so its evidence never gets read. Skipping
    # them cuts the backlog ~10x without changing what's visible. ``min_score=1``
    # mirrors the dashboard's own filter (see frontend/.../analysis.ts).
    alert_clause = (
        ""
        if args.all_rows
        else "AND s.max_class != '' AND s.max_score >= 1"
    )
    # Risk-band filter via the in-table column. Bands are an ordered enum
    # (Low < Moderate < High < Critical); we encode the order in a fixed
    # dict so a future band rename is a one-line change.
    _BAND_ORDER = {"Low": 0, "Moderate": 1, "High": 2, "Critical": 3}
    if args.min_band:
        keep = [b for b, n in _BAND_ORDER.items() if n >= _BAND_ORDER[args.min_band]]
        keep_sql = ", ".join(f"'{b}'" for b in keep)
        band_clause = f"AND s.risk_band IN ({keep_sql})"
    else:
        band_clause = ""
    # Empty-evidence filter: by default only touch rows that haven't been
    # backfilled. ``--force`` ignores it so an evidence-shape fix can
    # refresh rows that were backfilled with the old buggy values.
    evidence_clause = (
        ""
        if args.force
        else "AND (s.evidence = '' OR s.evidence = '{{}}' OR s.evidence IS NULL)"
    )
    # Count-only path: cheap COUNT() against tx_class_scores alone,
    # without the JOIN to transactions or the big raw_data payload.
    if args.count_only:
        count_rows = client.execute(
            f"""
            SELECT count() FROM (SELECT * FROM tx_class_scores FINAL) AS s
            WHERE s.network = %(network)s
              {evidence_clause}
              {alert_clause}
              {band_clause}
              {days_clause}
            """,
            {"network": args.network},
        )
        total = count_rows[0][0] if count_rows else 0
        print(f"Matching rows: {total} (network={args.network})")
        return

    rows = client.execute(
        f"""
        SELECT s.tx_hash, s.network, s.max_score, s.max_class, s.analyzed_at,
               t.fee, t.input_count, t.output_count, t.total_output_value,
               t.addresses, t.metadata, t.raw_data, t.slot, t.block_height, t.timestamp
        FROM (SELECT * FROM tx_class_scores FINAL) AS s
        JOIN transactions t ON t.tx_hash = s.tx_hash AND t.network = s.network
        WHERE s.network = %(network)s
          {evidence_clause}
          {alert_clause}
          {band_clause}
          {days_clause}
        ORDER BY s.analyzed_at DESC, s.tx_hash ASC
        {limit_clause}
        """,
        {"network": args.network},
    )

    if not rows:
        print(f"No rows missing evidence on network={args.network}. Nothing to do.")
        return

    print(f"Found {len(rows)} rows missing evidence on network={args.network}.")
    scorers = _build_scorers()

    # Hydrate feature rows once so we can batch the cross-tx enrichment
    # passes (collisions / cycles / sandwich) without re-iterating ClickHouse
    # results twice.
    feature_rows = []
    metadata_by_tx = {}
    for row in rows:
        (tx_hash, network, prev_max, prev_class, prev_at,
         fee, in_n, out_n, total_out, addrs, metadata_s, raw_s, slot, bh, ts) = row

        try:
            raw = json.loads(raw_s) if isinstance(raw_s, str) else raw_s
        except (json.JSONDecodeError, TypeError):
            raw = {}
        try:
            meta = json.loads(metadata_s) if isinstance(metadata_s, str) and metadata_s else None
        except (json.JSONDecodeError, TypeError):
            meta = None

        feature_rows.append({
            "tx_hash": tx_hash,
            "network": network,
            "fee": fee,
            "input_count": in_n,
            "output_count": out_n,
            "total_output_value": total_out,
            "metadata": meta,
            "addresses": list(addrs) if addrs else [],
            "raw_data": raw,
            "slot": slot,
            "block_height": bh,
            "timestamp": ts,
        })
        metadata_by_tx[tx_hash] = (prev_max, prev_class, prev_at)

    # Resolve input addresses before scoring: raw_data.inputs only carries
    # (tx_hash, output_index) refs, the actual addresses live in
    # transaction_inputs. Without this, scorers that group inputs by script
    # (multiple_sat) see all-empty addresses and silently drop alerts.
    _enrich_inputs_with_resolved_addresses(feature_rows, args.network)

    # Cross-tx enrichment for front_running / sandwich / circular.
    # Skipping this would leave those classes' evidence empty even after a
    # re-score, since their gates depend on collision / sandwich / cycle
    # features attached out-of-band.
    if settings.SCORER_FRONT_RUNNING_ENABLED:
        tx_hashes = [r["tx_hash"] for r in feature_rows]
        try:
            collisions = asyncio.run(
                postgres.get_collisions_for_txs(tx_hashes, args.network)
            )
        except Exception as e:
            logger.warning(f"Collision enrichment failed (non-fatal): {e}")
            collisions = {}
        for fr in feature_rows:
            collision = collisions.get(fr["tx_hash"])
            if collision:
                fr["collision"] = collision

    _enrich_cycle_features(feature_rows, args.network)
    _enrich_sandwich_features(feature_rows, args.network)

    corrected = []
    score_drifted = 0

    for feature_row in feature_rows:
        tx_hash = feature_row["tx_hash"]
        prev_max, prev_class, prev_at = metadata_by_tx[tx_hash]

        result = _score_transaction(feature_row, scorers)
        # Same calendar-day partition as the original row so the
        # ReplacingMergeTree dedupe kicks in; bump by 1s so the new row
        # wins max(analyzed_at).
        result["analyzed_at"] = prev_at + timedelta(seconds=1)
        corrected.append(result)

        if (
            round(float(result["max_score"]), 2) != round(float(prev_max), 2)
            or result["max_class"] != prev_class
        ):
            score_drifted += 1
            print(
                f"  drift {tx_hash}: "
                f"{prev_class or '(none)'} ({prev_max:.2f}) -> "
                f"{result['max_class'] or '(none)'} ({result['max_score']:.2f})"
            )

    print(
        f"\nProcessed {len(corrected)} rows; "
        f"{score_drifted} had score/class drift, "
        f"{len(corrected) - score_drifted} unchanged (evidence-only update)."
    )

    if not args.apply:
        print("Dry run. Pass --apply to insert.")
        return

    clickhouse.insert_class_scores(corrected)
    # Force a merge per affected partition so the dedupe takes effect now
    # instead of waiting for background merges.
    partitions = sorted({r["analyzed_at"].strftime("%Y%m%d") for r in corrected})
    for part in partitions:
        client.execute(
            f"OPTIMIZE TABLE tx_class_scores PARTITION {part} FINAL"
        )
    print(f"Inserted {len(corrected)} rows; merged partitions: {', '.join(partitions)}")


if __name__ == "__main__":
    sys.exit(main())
