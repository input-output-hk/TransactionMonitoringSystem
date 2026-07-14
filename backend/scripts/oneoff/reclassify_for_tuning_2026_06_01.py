"""Re-classify alert rows by re-running the FULL analysis engine, applying all
of the 2026-06-01/02 detection tuning at once. Meant to be run on the server.

The tuning changes this re-score applies (all in the live scorers/config
already; the 06-02 multiple_sat and sandwich-bracketing work landed a day after
the original 06-01 set, so older stored rows predate them):
  - token_dust:   gate suppresses sub-dos_asset_min bundles;
  - large_value:  digits-floor cap holds normal-supply UTxOs to Informational;
  - large_datum:  entropy + leaf-concentration + size-backstop gate replaces the
                  raw byte gate;
  - sandwich:     temporal bracketing via (slot, block_index) + net-ADA-profit +
                  non-script-attacker suppression;
  - circular:     structural-only cycles suppressed;
  - multiple_sat: per-script value-extraction baselines (skip the global tier) +
                  per-script extraction headroom + uniform-sweep / value-returned
                  state-continuation suppression.

This re-runs ``engine._score_transaction`` with the SAME enrichment chain
``run_once`` uses (resolved inputs, cycles, sandwich, collisions), so every
class is scored correctly, not just the changed ones, and there is no
preserve-vs-stale subtlety. Connection comes from settings, so it runs unchanged
on the server.

Scope: the default is Moderate-or-above rows (the alert surface), which was
sufficient for the 06-01/02 tuning because every change in that set was
suppressive (it only lowered scores), so an Informational/none row could not
become an alert. THAT NO LONGER HOLDS for changes landed on or after
2026-06-11: the recall fixes (anchor-relative p50 poisoning bound, drift-guard
polarity, escape floor) are recall-POSITIVE, meaning previously silenced or
low-banded rows can now score into the alert bands. To pick those up you MUST
pass --all-bands; the Moderate+ default would skip exactly the rows the fixes
exist to un-silence.

tx_class_scores is a ReplacingMergeTree keyed on (network, tx_hash) deduped by
max(analyzed_at); re-inserting with analyzed_at bumped +1s supersedes the old
row (the v2 table is unpartitioned).

  python -m scripts.oneoff.reclassify_for_tuning_2026_06_01 --network preprod              # dry-run
  python -m scripts.oneoff.reclassify_for_tuning_2026_06_01 --network preprod --count-only
  python -m scripts.oneoff.reclassify_for_tuning_2026_06_01 --network preprod --apply       # write
"""

import argparse
import asyncio
import sys
from datetime import timedelta

from app.analysis.engine import (
    _build_scorers,
    _enrich_cycle_features,
    _enrich_inputs_with_resolved_addresses,
    _enrich_sandwich_features,
    _score_transaction,
)
from app.config import settings
from app.db import postgres
from scripts.oneoff import _rescore_common as rc

# Bounds the ``WHERE tx_hash IN (...)`` literal that input-resolution builds, so
# it stays under ClickHouse's default 256 KB max_query_size (~500 hashes ~ 33 KB).
_CHUNK = 500


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default=settings.CARDANO_NETWORK)
    ap.add_argument(
        "--all-bands",
        action="store_true",
        help="also re-score Informational/none rows (cosmetic, slower)",
    )
    ap.add_argument("--limit", type=int, default=0, help="cap rows (smoke test)")
    ap.add_argument("--apply", action="store_true", help="insert corrected rows")
    ap.add_argument("--count-only", action="store_true", help="print the row count and exit")
    args = ap.parse_args()

    client = rc.connect()

    band_clause = "" if args.all_bands else "AND risk_band IN ('Moderate','High','Critical')"
    where = f"network = %(n)s {band_clause}"

    cnt = client.execute(
        f"SELECT count() FROM tx_class_scores FINAL WHERE {where}",
        {"n": args.network},
    )[0][0]
    label = "all classified" if args.all_bands else "Moderate+"
    print(f"{label} rows ({args.network}): {cnt}")
    if args.count_only:
        return

    limit_sql = f"LIMIT {args.limit}" if args.limit else ""
    # Fetch only ids + previous max_class up front (cheap, no raw_data), so the
    # heavy raw_data is pulled one chunk at a time below and peak memory stays
    # flat regardless of how many alert rows there are.
    meta = client.execute(
        f"""
        SELECT tx_hash, max_class, analyzed_at
        FROM tx_class_scores FINAL WHERE {where}
        ORDER BY analyzed_at, tx_hash
        {limit_sql}
        """,
        {"n": args.network},
    )
    if not meta:
        print("No matching rows.")
        return
    hashes = [r[0] for r in meta]
    prev_at = {r[0]: r[2] for r in meta}
    prev_cls = {r[0]: r[1] for r in meta}

    # Collision enrichment (front_running) once, for all hashes, in a single
    # event loop (the asyncpg pool binds to it). If it fails, front_running
    # re-scores to nothing and would OVERWRITE real findings, so we refuse to
    # --apply; a dry-run continues with a warning so the rest can be previewed.
    collisions = {}
    if settings.SCORER_FRONT_RUNNING_ENABLED:
        try:
            collisions = asyncio.run(postgres.get_collisions_for_txs(hashes, args.network))
        except Exception as exc:
            print(f"collision enrichment failed: {exc}", file=sys.stderr)
            if args.apply:
                print(
                    "Refusing to --apply: front_running findings would be dropped. "
                    "Re-run with Postgres reachable, or unset SCORER_FRONT_RUNNING_ENABLED.",
                    file=sys.stderr,
                )
                return 1
            print(
                "Dry run continues WITHOUT collisions (front_running preview is incomplete).",
                file=sys.stderr,
            )

    scorers = _build_scorers()
    corrected, prev_classes = [], []
    for i in range(0, len(hashes), _CHUNK):
        chunk_hashes = hashes[i : i + _CHUNK]
        tx_rows = client.execute(
            """
            SELECT tx_hash, fee, input_count, output_count, total_output_value,
                   addresses, metadata, raw_data, slot, block_height, timestamp
            FROM transactions FINAL WHERE network = %(n)s AND tx_hash IN %(h)s
            """,
            {"n": args.network, "h": chunk_hashes},
        )
        chunk = []
        for tx_hash, fee, in_n, out_n, total_out, addrs, meta_s, raw_s, slot, bh, ts in tx_rows:
            fr = {
                "tx_hash": tx_hash,
                "network": args.network,
                "fee": fee,
                "input_count": in_n,
                "output_count": out_n,
                "total_output_value": total_out,
                "metadata": rc.loads(meta_s, None),
                "addresses": list(addrs) if addrs else [],
                "raw_data": rc.loads(raw_s, {}),
                "slot": slot,
                "block_height": bh,
                "timestamp": ts,
            }
            collision = collisions.get(tx_hash)
            if collision:
                fr["collision"] = collision
            chunk.append(fr)

        _enrich_inputs_with_resolved_addresses(chunk, args.network)
        _enrich_cycle_features(chunk, args.network)
        _enrich_sandwich_features(chunk, args.network)
        for fr in chunk:
            result = _score_transaction(fr, scorers)
            result["analyzed_at"] = prev_at[fr["tx_hash"]] + timedelta(seconds=1)
            corrected.append(result)
            prev_classes.append(prev_cls[fr["tx_hash"]])
        print(f"  ...scored {min(i + _CHUNK, len(hashes))}/{len(hashes)}", flush=True)

    rc.report(corrected, prev_classes)
    if not args.apply:
        print("\nDry run. Pass --apply to write.")
        return
    rc.write(client, corrected)


if __name__ == "__main__":
    sys.exit(main())
