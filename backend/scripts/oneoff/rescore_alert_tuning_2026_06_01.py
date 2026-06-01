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

tx_class_scores is a ReplacingMergeTree keyed on (network, tx_hash) deduped by
max(analyzed_at); re-inserting with analyzed_at bumped +1s (same calendar-day
partition) supersedes the old row.

Dry-run by default; pass --apply to write. --days N limits to rows analyzed in
the last N days; --count-only just reports the target row count.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import timedelta

from clickhouse_driver import Client

from app.analysis.engine import _enrich_sandwich_features
from app.analysis.normalise import score_to_band
from app.analysis.scorers.token_dust import TokenDustScorer
from app.analysis.scorers.large_datum import LargeDatumScorer
from app.analysis.scorers.sandwich import SandwichScorer
from app.db import clickhouse


def _run_scorer(scorer, features):
    """Return (score, sub_scores, evidence); score -1 when the gate is closed."""
    try:
        if scorer.gate(features):
            r = scorer.score(features)
            return r.score, r.sub_scores, r.evidence
    except Exception as exc:  # pragma: no cover - defensive, logged to stderr
        print(f"  scorer {scorer.name} failed on {features.get('tx_hash')}: {exc}",
              file=sys.stderr)
    return -1.0, None, None


def _merge(sub_scores, evidence, name, score, sub, evd):
    """Apply a re-run class result onto the preserved sub_scores/evidence dicts."""
    if score < 0:
        sub_scores.pop(name, None)
        evidence.pop(name, None)
        return
    if sub is not None:
        sub_scores[name] = sub
    if evd:
        evidence[name] = evd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="preprod")
    ap.add_argument("--days", type=int, default=0,
                    help="limit to rows analyzed within the last N days (0 = all history)")
    ap.add_argument("--limit", type=int, default=0, help="cap rows processed (smoke test)")
    ap.add_argument("--apply", action="store_true", help="insert corrected rows")
    ap.add_argument("--count-only", action="store_true", help="just report the target count")
    args = ap.parse_args()

    client = Client(host="localhost", port=9000, user="default", password="",
                    database="tms_analytics")

    where = ["network = %(net)s",
             "max_class IN ('token_dust','large_datum','sandwich')"]
    params = {"net": args.network}
    if args.days:
        where.append("analyzed_at >= now() - INTERVAL %(days)s DAY")
        params["days"] = args.days
    where_sql = " AND ".join(where)

    cnt = client.execute(
        f"SELECT count() FROM (SELECT * FROM tx_class_scores FINAL) WHERE {where_sql}",
        params,
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
               t.fee, t.input_count, t.output_count, t.total_output_value,
               t.addresses, t.metadata, t.raw_data, t.slot, t.block_height, t.timestamp
        FROM (SELECT * FROM tx_class_scores FINAL WHERE {where_sql}) AS s
        ANY LEFT JOIN transactions t ON t.tx_hash = s.tx_hash AND t.network = s.network
        ORDER BY s.analyzed_at, s.tx_hash
        {limit_sql}
        """,
        params,
    )

    td, ld, sw = TokenDustScorer(), LargeDatumScorer(), SandwichScorer()
    corrected = []
    changed = 0

    for row in rows:
        (tx_hash, network, c_td, c_lv, c_ld, c_ms, c_fr, c_sw, c_ci, c_ft, c_ph,
         sub_s, ev_s, ver, prev_at, prev_class,
         fee, in_n, out_n, total_out, addrs, meta_s, raw_s, slot, bh, ts) = row

        raw = json.loads(raw_s) if isinstance(raw_s, str) and raw_s else (raw_s or {})
        meta = None
        if isinstance(meta_s, str) and meta_s and meta_s != "{}":
            try:
                meta = json.loads(meta_s)
            except Exception:
                meta = None
        try:
            sub_scores = json.loads(sub_s) if isinstance(sub_s, str) and sub_s else {}
        except Exception:
            sub_scores = {}
        try:
            evidence = json.loads(ev_s) if isinstance(ev_s, str) and ev_s else {}
        except Exception:
            evidence = {}

        features = {
            "tx_hash": tx_hash, "network": network, "fee": fee,
            "input_count": in_n, "output_count": out_n,
            "total_output_value": total_out, "metadata": meta,
            "addresses": list(addrs) if addrs else [], "raw_data": raw,
            "slot": slot, "block_height": bh, "timestamp": ts,
        }

        scores = {
            "token_dust": c_td, "large_value": c_lv, "large_datum": c_ld,
            "multiple_sat": c_ms, "front_running": c_fr, "sandwich": c_sw,
            "circular": c_ci, "fake_token": c_ft, "phishing": c_ph,
        }

        # Re-run the two raw_data-only scorers.
        for name, scorer in (("token_dust", td), ("large_datum", ld)):
            sc, ss, evd = _run_scorer(scorer, features)
            scores[name] = sc
            _merge(sub_scores, evidence, name, sc, ss, evd)

        # Re-run sandwich only where it previously fired (change can only remove).
        if c_sw >= 0:
            _enrich_sandwich_features([features], network)
            sc, ss, evd = _run_scorer(sw, features)
            scores["sandwich"] = sc
            _merge(sub_scores, evidence, "sandwich", sc, ss, evd)

        applicable = {k: v for k, v in scores.items() if v >= 0}
        if applicable:
            max_class = max(applicable, key=applicable.get)
            max_score = applicable[max_class]
        else:
            max_class, max_score = "", 0.0
        risk_band = score_to_band(max_score)

        corrected.append({
            "tx_hash": tx_hash, "network": network, **scores,
            "max_score": round(max_score, 2), "max_class": max_class,
            "risk_band": risk_band, "sub_scores": sub_scores, "evidence": evidence,
            "analysis_version": ver, "analyzed_at": prev_at + timedelta(seconds=1),
        })
        if max_class != prev_class:
            changed += 1

    print(f"re-scored {len(corrected)} rows; max_class changed on {changed}")
    print("new risk_band:", dict(Counter(r["risk_band"] for r in corrected)))
    print("new max_class:", dict(Counter(r["max_class"] or "(none)" for r in corrected)))

    if not args.apply:
        print("\nDry run. Pass --apply to write.")
        return

    clickhouse.insert_class_scores(corrected)
    partitions = sorted({r["analyzed_at"].strftime("%Y%m%d") for r in corrected})
    for part in partitions:
        client.execute(f"OPTIMIZE TABLE tx_class_scores PARTITION {part} FINAL")
    print(f"\nInserted {len(corrected)} corrected rows; merged {len(partitions)} partitions.")


if __name__ == "__main__":
    sys.exit(main())
