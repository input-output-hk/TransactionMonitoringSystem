"""Seed ``asset_policy_first_seen`` from historical rows (one-off, 2026-07).

The table is populated inline by ingestion from this release onward; this
script back-fills the sightings that predate it, from two sources:

  1. ``transaction_outputs.assets`` (flattened ``{"<policy>.<name>": qty}``
     JSON): every policy that ever appeared in an output value bundle.
  2. ``tx_script_features.mint_entries``: mint-map policies, covering the
     mint-and-burn-in-one-tx corner where a policy never reaches an output.

Both passes run as single server-side aggregations (GROUP BY policy) rather
than Python-side chunking: the row streams stay inside ClickHouse and the
result cardinality is bounded by the distinct-policy count, not the row
count. Idempotent: the AggregatingMergeTree keeps min(first_slot) per
(network, policy_id) whatever the insert order, so re-runs and overlap with
live ingestion are harmless. Slotless rows (mempool-only) are skipped; the
confirming block carries the slot.

  python -m scripts.oneoff.backfill_asset_policy_first_seen --network mainnet             # dry-run counts
  python -m scripts.oneoff.backfill_asset_policy_first_seen --network mainnet --apply     # write
"""

import argparse
import sys

from scripts.oneoff import _rescore_common as rc

# One-off aggregation over the full history; the default 300s ceiling is
# sized for interactive queries, not a whole-table GROUP BY on mainnet.
_MAX_EXECUTION_SECONDS = 3600

# The flattened assets key is "<policy_id>.<asset_name>"; the policy id is a
# 28-byte script hash, so its hex form is exactly 56 characters.
_POLICY_HEX_CHARS = 56

_OUTPUTS_SELECT = f"""
    SELECT
        o.network                              AS network,
        substring(k, 1, {_POLICY_HEX_CHARS})   AS policy_id,
        min(assumeNotNull(t.slot))             AS first_slot
    FROM transaction_outputs AS o
    INNER JOIN transactions AS t
        ON t.network = o.network AND t.tx_hash = o.tx_hash
    ARRAY JOIN JSONExtractKeys(o.assets) AS k
    WHERE o.network = %(n)s
      AND o.assets != ''
      AND t.slot IS NOT NULL
      AND length(k) >= {_POLICY_HEX_CHARS}
    GROUP BY network, policy_id
"""

_MINTS_SELECT = """
    SELECT
        f.network                                          AS network,
        JSONExtractString(entry, 'policy_id')              AS policy_id,
        min(assumeNotNull(t.slot))                         AS first_slot
    FROM tx_script_features AS f
    INNER JOIN transactions AS t
        ON t.network = f.network AND t.tx_hash = f.tx_hash
    ARRAY JOIN JSONExtractArrayRaw(f.mint_entries) AS entry
    WHERE f.network = %(n)s
      AND f.mint_entries != ''
      AND t.slot IS NOT NULL
      AND policy_id != ''
    GROUP BY network, policy_id
"""

_SOURCES = (("outputs", _OUTPUTS_SELECT), ("mints", _MINTS_SELECT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", required=True)
    ap.add_argument("--apply", action="store_true", help="write the aggregated sightings")
    args = ap.parse_args()

    client = rc.connect()
    settings = {"max_execution_time": _MAX_EXECUTION_SECONDS}

    before = client.execute(
        "SELECT count() FROM (SELECT policy_id FROM asset_policy_first_seen "
        "WHERE network = %(n)s GROUP BY policy_id)",
        {"n": args.network},
    )[0][0]
    print(f"asset_policy_first_seen distinct policies ({args.network}): {before}")

    for label, select in _SOURCES:
        if args.apply:
            client.execute(
                "INSERT INTO asset_policy_first_seen (network, policy_id, first_slot) " + select,
                {"n": args.network},
                settings=settings,
            )
            print(f"  {label}: inserted")
        else:
            cnt = client.execute(
                f"SELECT count() FROM ({select})",
                {"n": args.network},
                settings=settings,
            )[0][0]
            print(f"  {label}: {cnt} distinct (network, policy) sightings (dry-run)")

    if args.apply:
        after = client.execute(
            "SELECT count() FROM (SELECT policy_id FROM asset_policy_first_seen "
            "WHERE network = %(n)s GROUP BY policy_id)",
            {"n": args.network},
        )[0][0]
        print(f"distinct policies after backfill: {after} (was {before})")
    else:
        print("\nDry run. Pass --apply to write.")


if __name__ == "__main__":
    sys.exit(main())
