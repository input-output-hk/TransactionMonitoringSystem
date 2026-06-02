"""Pass-2 historical re-score: apply the large_value, circular, and new
large_datum tuning to existing preprod rows.

Only the CHANGED scorers are recomputed per row; the unchanged classes
(sandwich, multiple_sat, front_running, fake_token, phishing) are preserved
exactly as stored, so this applies the intended changes without perturbing them
via baseline drift.

  - token_dust / large_value / large_datum: re-run (pure raw_data scorers).
    token_dust is re-run because earlier partial rescores left stale Moderate
    values on rows where it was not the max class; large_value's digits-floor
    cap holds normal-supply UTxOs to top-of-Low; large_datum's entropy +
    size-backstop + leaf-concentration gate replaces the old byte gate and can
    RESURRECT a row the byte gate suppressed (e.g. CTF-04 at 7.3 KB), so every
    datum-bearing tx is in scope, not just current large_datum rows.
  - circular: structural-only suppression is recomputed from the STORED
    sub_scores (recipient_entropy_inv + auxiliary + speed <
    structural_corroboration_floor), so no cycle re-enrichment is needed.

Sandwich is preserved (it needs dex enrichment and was suppressed in Pass 1;
run this script after rescore_alert_tuning_2026_06_01 on a fresh DB).

Shared fetch/recompute/write logic lives in _rescore_common. Dry-run by
default; --apply to write.

NOTE: this is the LOCAL incremental rescore (Pass 2 of 2, run after the Pass-1
script). For a fresh DB such as the server, run reclassify_for_tuning_2026_06_01
instead: it applies all of today's tuning in one full re-analysis.
"""

import argparse
import sys

from app.analysis.scorers.circular import _STRUCTURAL_CORROBORATION_FLOOR
from app.analysis.scorers.large_datum import LargeDatumScorer
from app.analysis.scorers.large_value import LargeValueScorer
from app.analysis.scorers.token_dust import TokenDustScorer

from scripts.oneoff import _rescore_common as rc


def _circular_structural_only(sub_scores) -> bool:
    """A circular finding with no corroborating evidence (entropy/auxiliary/speed
    below the floor) is structurally indistinguishable from benign DeFi; it is
    suppressed. Computed from the stored sub_scores, no cycle re-enrichment."""
    cs = sub_scores.get("circular")
    if not isinstance(cs, dict):
        return False
    corroboration = (
        float(cs.get("recipient_entropy_inv", 0.0))
        + float(cs.get("auxiliary", 0.0))
        + float(cs.get("speed", 0.0))
    )
    return corroboration < _STRUCTURAL_CORROBORATION_FLOOR


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="preprod")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--count-only", action="store_true")
    args = ap.parse_args()

    client = rc.connect()

    # Scope: only rows the three re-run scorers can move, plus large_datum
    # resurrection candidates (every tx carrying a sizeable inline datum, found
    # via raw_data length, regardless of current max_class). Everything else is
    # provably unchanged and skipped. large_value/token_dust bottom-band rows are
    # excluded because capping/suppressing a bottom-band score cannot change its
    # band. ('Low' is the pre-2026-06 label for the Informational band.)
    scope = """network=%(n)s AND (
        (max_class IN ('large_value','token_dust') AND risk_band NOT IN ('Informational', 'Low'))
        OR max_class IN ('circular','large_datum')
        OR tx_hash IN (
            SELECT tx_hash FROM transactions
            WHERE network=%(n)s AND length(raw_data) >= 10000
              AND position(raw_data, 'datum') > 0
        )
    )"""

    cnt = client.execute(
        f"SELECT count() FROM tx_class_scores FINAL WHERE {scope}", {"n": args.network},
    )[0][0]
    print(f"in-scope rows ({args.network}): {cnt}")
    if args.count_only:
        return

    limit_sql = f"LIMIT {args.limit}" if args.limit else ""
    rows = client.execute(
        f"""
        SELECT s.tx_hash, s.network,
               s.token_dust, s.large_value, s.large_datum, s.multiple_sat,
               s.front_running, s.sandwich, s.circular, s.fake_token, s.phishing,
               s.sub_scores, s.evidence, s.analysis_version, s.analyzed_at, s.max_class,
               t.raw_data
        FROM (SELECT * FROM tx_class_scores FINAL WHERE {scope}) AS s
        ANY LEFT JOIN transactions t ON t.tx_hash = s.tx_hash AND t.network = s.network
        ORDER BY s.analyzed_at, s.tx_hash
        {limit_sql}
        """,
        {"n": args.network},
    )

    td, lv, ld = TokenDustScorer(), LargeValueScorer(), LargeDatumScorer()
    corrected, prev_classes = [], []

    for row in rows:
        (tx_hash, network, c_td, c_lv, c_ld, c_ms, c_fr, c_sw, c_ci, c_ft, c_ph,
         sub_s, ev_s, ver, prev_at, prev_class, raw_s) = row

        features = {"tx_hash": tx_hash, "network": network, "raw_data": rc.loads(raw_s, {})}
        sub_scores = rc.loads(sub_s, {})
        evidence = rc.loads(ev_s, {})

        scores = {
            "token_dust": c_td, "large_value": c_lv, "large_datum": c_ld,
            "multiple_sat": c_ms, "front_running": c_fr, "sandwich": c_sw,
            "circular": c_ci, "fake_token": c_ft, "phishing": c_ph,
        }

        # Re-run the pure raw_data scorers.
        for name, scorer in (("token_dust", td), ("large_value", lv), ("large_datum", ld)):
            sc, ss, evd = rc.run_scorer(scorer, features)
            scores[name] = sc
            rc.merge_class(sub_scores, evidence, name, sc, ss, evd)

        # Circular: suppress structural-only from stored sub_scores (no enrichment).
        if c_ci >= 0 and _circular_structural_only(sub_scores):
            scores["circular"] = -1.0
            sub_scores.pop("circular", None)
            evidence.pop("circular", None)

        corrected.append(
            rc.corrected_row(tx_hash, network, scores, sub_scores, evidence, ver, prev_at)
        )
        prev_classes.append(prev_class)

    rc.report(corrected, prev_classes)
    if not args.apply:
        print("\nDry run. Pass --apply to write.")
        return
    rc.write(client, corrected)


if __name__ == "__main__":
    sys.exit(main())
