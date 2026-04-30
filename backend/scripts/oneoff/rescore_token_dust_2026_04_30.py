"""Re-score the 2026-04-30 critical token_dust cluster after the gate +
degenerate-baseline fixes. Re-runs the full scoring engine for each affected
tx so adjacent classes (e.g. large_value, which shares the inverted-ADA axis)
also get corrected, then re-inserts the rows. ReplacingMergeTree dedupes on
(network, tx_hash) by max(analyzed_at), so a newer row in the same partition
supersedes the old one.

Run with --apply to write; default is dry-run.
"""

import argparse
import json
import sys
from datetime import timedelta

from clickhouse_driver import Client

from app.analysis.engine import _build_scorers, _score_transaction
from app.db import clickhouse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Insert corrected rows")
    args = parser.parse_args()

    client = Client(host="localhost", port=9000, user="default", password="", database="tms_analytics")

    rows = client.execute(
        """
        SELECT s.tx_hash, s.network, s.max_score, s.max_class, s.risk_band, s.analyzed_at,
               t.fee, t.input_count, t.output_count, t.total_output_value,
               t.addresses, t.metadata, t.raw_data, t.slot, t.block_height, t.timestamp
        FROM (SELECT * FROM tms_analytics.tx_class_scores FINAL) AS s
        JOIN tms_analytics.transactions t ON t.tx_hash = s.tx_hash AND t.network = s.network
        WHERE s.max_class = 'token_dust' AND s.risk_band = 'Critical'
        ORDER BY s.analyzed_at, s.tx_hash
        """
    )

    scorers = _build_scorers()
    corrected = []

    print(f"{'tx_hash':<66} {'before':>20} {'after':>22}")
    for row in rows:
        (tx_hash, network, prev_max, prev_class, prev_band, prev_at,
         fee, in_n, out_n, total_out, addrs, metadata_s, raw_s, slot, bh, ts) = row

        try:
            raw = json.loads(raw_s) if isinstance(raw_s, str) else raw_s
        except Exception:
            raw = {}
        try:
            meta = json.loads(metadata_s) if isinstance(metadata_s, str) and metadata_s else None
        except Exception:
            meta = None

        feature_row = {
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
        }

        result = _score_transaction(feature_row, scorers)
        # ReplacingMergeTree dedupes within a partition only, and partitions
        # are toYYYYMMDD(analyzed_at). Keep the same calendar day as the
        # original row but bump the timestamp by 1s so the corrected row
        # wins the dedupe.
        result["analyzed_at"] = prev_at + timedelta(seconds=1)
        corrected.append(result)

        print(f"{tx_hash} {prev_class:>13} ({prev_max:>2.0f}) "
              f"-> {result['max_class'] or '(none)':>13} ({result['max_score']:>5.2f}) [{result['risk_band']}]")

    if not args.apply:
        print(f"\nDry run: {len(corrected)} rows would be re-inserted. Pass --apply to write.")
        return

    clickhouse.insert_class_scores(corrected)
    # Force a merge per affected partition so the dedupe takes effect
    # immediately rather than waiting for background merges.
    partitions = sorted({r["analyzed_at"].strftime("%Y%m%d") for r in corrected})
    for part in partitions:
        client.execute(f"OPTIMIZE TABLE tms_analytics.tx_class_scores PARTITION {part} FINAL")
    print(f"\nInserted {len(corrected)} corrected rows; merged partitions: {', '.join(partitions)}")


if __name__ == "__main__":
    sys.exit(main())
