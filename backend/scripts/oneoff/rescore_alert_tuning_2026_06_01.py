"""Re-score token_dust / sandwich / large_datum alert rows after the
2026-06-01 false-positive tuning:

  - token_dust: gate-level suppression (a bundle engages only with
    >= dos_asset_min pairs OR Value CBOR >= dos_value_cbor_min);
  - large_datum: byte gate raised to a maxTxSize-derived floor and weight
    shifted off the saturating datum_ratio onto absolute datum_bytes;
  - sandwich: suppressed unless the attacker is a non-script (wallet) cluster
    that netted >= min_profit_lovelace ADA across the two legs.

Only the three tuned scorers are re-run per row; the other six class scores are
preserved from the existing row (they were computed under full enrichment and
are unaffected by this change). max_score / max_class / risk_band are recomputed
from the merged vector. Sandwich is re-evaluated only for rows that previously
carried a sandwich finding, because the change can only remove sandwich
findings, never add them; those rows use the live per-row enrichment so a
genuinely profitable wallet-attacker sandwich (if any) survives.

Shared fetch/recompute/write logic lives in _rescore_common. ReplacingMergeTree
supersedes by bumped analyzed_at. Dry-run by default; --apply to write.

NOTE: this is the LOCAL incremental rescore (Pass 1 of 2; run the pass2 script
after it). For a fresh DB such as the server, run reclassify_for_tuning_2026_06_01
instead: it applies all of today's tuning in one full re-analysis.
"""

import argparse
import sys

from app.analysis.engine import _enrich_sandwich_features
from app.analysis.scorers.large_datum import LargeDatumScorer
from app.analysis.scorers.sandwich import SandwichScorer
from app.analysis.scorers.token_dust import TokenDustScorer

from scripts.oneoff import _rescore_common as rc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="preprod")
    ap.add_argument("--days", type=int, default=0,
                    help="limit to rows analyzed within the last N days (0 = all history)")
    ap.add_argument("--limit", type=int, default=0, help="cap rows processed (smoke test)")
    ap.add_argument("--apply", action="store_true", help="insert corrected rows")
    ap.add_argument("--count-only", action="store_true", help="just report the target count")
    args = ap.parse_args()

    client = rc.connect()

    where = ["network = %(net)s", "max_class IN ('token_dust','large_datum','sandwich')"]
    params = {"net": args.network}
    if args.days:
        where.append("analyzed_at >= now() - INTERVAL %(days)s DAY")
        params["days"] = args.days
    where_sql = " AND ".join(where)

    cnt = client.execute(
        f"SELECT count() FROM tx_class_scores FINAL WHERE {where_sql}", params,
    )[0][0]
    print(f"target rows ({args.network}, days={args.days or 'all'}): {cnt}")
    if args.count_only:
        return

    limit_sql = f"LIMIT {args.limit}" if args.limit else ""
    rows = client.execute(
        f"""
        SELECT s.tx_hash, s.network,
               s.token_dust, s.large_value, s.large_datum, s.multiple_sat,
               s.front_running, s.sandwich, s.circular, s.fake_token, s.phishing,
               s.sub_scores, s.evidence, s.analysis_version, s.analyzed_at, s.max_class,
               t.raw_data, t.slot
        FROM (SELECT * FROM tx_class_scores FINAL WHERE {where_sql}) AS s
        ANY LEFT JOIN transactions t ON t.tx_hash = s.tx_hash AND t.network = s.network
        ORDER BY s.analyzed_at, s.tx_hash
        {limit_sql}
        """,
        params,
    )

    td, ld, sw = TokenDustScorer(), LargeDatumScorer(), SandwichScorer()
    corrected, prev_classes = [], []

    for row in rows:
        (tx_hash, network, c_td, c_lv, c_ld, c_ms, c_fr, c_sw, c_ci, c_ft, c_ph,
         sub_s, ev_s, ver, prev_at, prev_class, raw_s, slot) = row

        # token_dust / large_datum read only raw_data; sandwich enrichment reads slot.
        features = {
            "tx_hash": tx_hash, "network": network,
            "raw_data": rc.loads(raw_s, {}), "slot": slot,
        }
        sub_scores = rc.loads(sub_s, {})
        evidence = rc.loads(ev_s, {})

        scores = {
            "token_dust": c_td, "large_value": c_lv, "large_datum": c_ld,
            "multiple_sat": c_ms, "front_running": c_fr, "sandwich": c_sw,
            "circular": c_ci, "fake_token": c_ft, "phishing": c_ph,
        }

        # Re-run the two raw_data-only scorers.
        for name, scorer in (("token_dust", td), ("large_datum", ld)):
            sc, ss, evd = rc.run_scorer(scorer, features)
            scores[name] = sc
            rc.merge_class(sub_scores, evidence, name, sc, ss, evd)

        # Re-run sandwich only where it previously fired (change can only remove).
        if c_sw >= 0:
            _enrich_sandwich_features([features], network)
            sc, ss, evd = rc.run_scorer(sw, features)
            scores["sandwich"] = sc
            rc.merge_class(sub_scores, evidence, "sandwich", sc, ss, evd)

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
