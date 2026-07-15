"""Data migration: rename the stored risk_band "Low" -> "Informational".

The 0-30 band was relabelled "Low" -> "Informational" (clients read "Low" as a
low-grade threat; "Informational" reads as "nothing to act on"). Scores and
thresholds are unchanged, so this is a pure string rewrite of the denormalised
``risk_band`` column on historical rows; nothing needs re-scoring.

``RiskBand._missing_`` already maps a legacy "Low" onto INFORMATIONAL, so API
reads never break regardless of whether this migration has run. This script just
makes the stored values match the new label so direct ClickHouse consumers (and
band-count aggregations) see "Informational".

Idempotent: a second run matches zero rows. Dry-run by default; --apply executes
the mutation and waits for it to materialise (mutations_sync). Runs unchanged on
the server (connection from settings via _rescore_common).

  python -m scripts.oneoff.migrate_risk_band_low_to_informational              # dry-run
  python -m scripts.oneoff.migrate_risk_band_low_to_informational --apply      # migrate
"""

import argparse
import sys

from scripts.oneoff import _rescore_common as rc

_OLD_LABEL = "Low"
_NEW_LABEL = "Informational"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--network", default=None, help="restrict to one network (default: all networks)"
    )
    ap.add_argument("--apply", action="store_true", help="execute the mutation")
    args = ap.parse_args()

    client = rc.connect()

    where = "risk_band = %(old)s"
    params = {"old": _OLD_LABEL, "new": _NEW_LABEL}
    if args.network:
        where += " AND network = %(net)s"
        params["net"] = args.network

    remaining = client.execute(
        f"SELECT count() FROM tx_class_scores FINAL WHERE {where}",
        params,
    )[0][0]
    scope = args.network or "all networks"
    print(f"rows still labelled '{_OLD_LABEL}' ({scope}): {remaining}")
    if remaining == 0:
        print("Nothing to migrate.")
        return
    if not args.apply:
        print(f"\nDry run. Pass --apply to rewrite them to '{_NEW_LABEL}'.")
        return

    # mutations_sync=2: wait until the mutation has materialised on all replicas
    # before returning, so a follow-up read sees the migrated values.
    client.execute(
        f"ALTER TABLE tx_class_scores UPDATE risk_band = %(new)s WHERE {where}",
        params,
        settings={"mutations_sync": 2},
    )
    left = client.execute(
        f"SELECT count() FROM tx_class_scores FINAL WHERE {where}",
        params,
    )[0][0]
    print(f"Migrated. Rows still labelled '{_OLD_LABEL}': {left}")


if __name__ == "__main__":
    sys.exit(main())
