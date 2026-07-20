"""Re-score historical rows for the multiple_sat saturation band floor
(2026-07). Meant to be run on the server after the floor deploys.

The change this re-score applies: multiple_sat gained a saturated-axes band
floor (``scorers.multiple_sat.saturation_floor``). When the un-widened
extraction floor signal and s_inputs both saturate on a non-allowlisted,
non-uniform-sweep script group, the score floors into High. Before the
floor, the heavy-CPU double-satisfaction corner was structurally capped at
w_extraction + w_inputs = 58.0, two points below the High threshold, so the
scorer's strongest heavy-CPU detections could never page.

This change is recall-POSITIVE: scores can only rise, and previously
escape-capped (exactly-Moderate) or 58.0-capped rows can cross into High.
Per the scope rule documented in reclassify_for_tuning_2026_06_01, you MUST
pass ``--all-bands``: the Moderate+ default would still catch the known
58.0 cohort, but suppression-escape rows that re-band upward can start from
any stored band, and skipping them defeats the point of the backfill.

Expected mainnet effect (measured 2026-07-20): nine txs stored at exactly
58.0 re-band Moderate -> High with the ``saturation_band_floor`` reason.
Triage every promoted tx before acting on it; if a legitimate batcher
trips the floor, the remedy is a network-scoped
``multiple_sat.allowlist_prefixes`` entry with a REVIEW BY date, never a
floor-threshold change without the recall-gate protocol.

Mechanics are identical to the canonical full-engine re-run (same
enrichment chain, same ReplacingMergeTree supersede-by-analyzed_at+1s), so
this entry point delegates to it; see that module's docstring for the
operational details.

  python -m scripts.oneoff.rescore_saturation_floor_2026_07 --network mainnet --count-only
  python -m scripts.oneoff.rescore_saturation_floor_2026_07 --network mainnet --all-bands           # dry-run
  python -m scripts.oneoff.rescore_saturation_floor_2026_07 --network mainnet --all-bands --apply   # write
"""

import sys

from scripts.oneoff.reclassify_for_tuning_2026_06_01 import main

if __name__ == "__main__":
    if "--apply" in sys.argv and "--all-bands" not in sys.argv:
        print(
            "Refusing to --apply without --all-bands: this change is "
            "recall-positive and low-banded rows can re-band upward "
            "(see module docstring).",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(main())
