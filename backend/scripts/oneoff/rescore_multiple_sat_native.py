"""Re-score elevated multiple_sat alerts after the native-script gate fix.

Targets all preprod txs where ``max_class == 'multiple_sat'`` and
``risk_band IN ('Moderate', 'High', 'Critical')``. Native-script (multisig /
timelock) addresses are now excluded by the gate, so consolidation txs at
those addresses re-score to whichever class is actually applicable (often
none, falling back to Low).

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
        WHERE s.network = 'preprod'
          AND s.max_class = 'multiple_sat'
          AND s.risk_band IN ('Moderate', 'High', 'Critical')
        ORDER BY s.analyzed_at, s.tx_hash
        """
    )

    scorers = _build_scorers()
    corrected = []
    band_changed = 0

    print(f"{'tx_hash':<66} {'before':>22} {'after':>22}")
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
        result["analyzed_at"] = prev_at + timedelta(seconds=1)
        corrected.append(result)

        if result["risk_band"] != prev_band or result["max_class"] != prev_class:
            band_changed += 1
        print(f"{tx_hash} {prev_class:>13} ({prev_max:>5.2f}) [{prev_band:>8}] -> "
              f"{(result['max_class'] or '(none)'):>13} ({result['max_score']:>5.2f}) [{result['risk_band']:>8}]")

    print(f"\n{band_changed}/{len(corrected)} rows changed class or band.")

    if not args.apply:
        print("Dry run: pass --apply to write.")
        return

    # Insert per-partition to avoid the merge race observed when batching
    # writes across many older partitions.
    by_partition = {}
    for r in corrected:
        key = r["analyzed_at"].strftime("%Y%m%d")
        by_partition.setdefault(key, []).append(r)

    for part, batch in sorted(by_partition.items()):
        clickhouse.insert_class_scores(batch)
        client.execute(f"OPTIMIZE TABLE tms_analytics.tx_class_scores PARTITION {part} FINAL")
        print(f"  partition {part}: inserted {len(batch)} rows and merged.")

    print(f"\nInserted {len(corrected)} corrected rows across {len(by_partition)} partitions.")


if __name__ == "__main__":
    sys.exit(main())
