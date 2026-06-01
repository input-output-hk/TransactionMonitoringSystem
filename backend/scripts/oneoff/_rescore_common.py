"""Shared helpers for the tx_class_scores re-score one-offs.

Both ``rescore_alert_tuning_*.py`` scripts fetch alert rows, recompute a subset
of the nine class scores, preserve the rest, and write corrected rows back. The
correctness-critical pieces, aggregate recompute, sub_scores/evidence merge, and
the ReplacingMergeTree re-insert + per-partition OPTIMIZE, live here so the two
scripts cannot drift. Per-script differences (which scorers re-run, the row
scope, any enrichment) stay in each script.
"""

import json
import sys
from collections import Counter
from datetime import timedelta

from clickhouse_driver import Client

from app.analysis.normalise import score_to_band
from app.config import settings
from app.db import clickhouse

CLASS_NAMES = (
    "token_dust", "large_value", "large_datum", "multiple_sat",
    "front_running", "sandwich", "circular", "fake_token", "phishing",
)


def connect() -> Client:
    """ClickHouse client from settings, so the same script runs unchanged
    against local preprod and the server (do not hardcode localhost)."""
    return Client(
        host=settings.CLICKHOUSE_HOST,
        port=settings.CLICKHOUSE_PORT,
        user=settings.CLICKHOUSE_USER,
        password=settings.CLICKHOUSE_PASSWORD,
        database=settings.CLICKHOUSE_DB,
    )


def loads(value, default):
    """Tolerant JSON parse for a stored string column; ``default`` on empty/bad."""
    if isinstance(value, str) and value and value != "{}":
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def run_scorer(scorer, features):
    """Return ``(score, sub_scores, evidence)``; ``-1`` when the gate is closed."""
    try:
        if scorer.gate(features):
            r = scorer.score(features)
            return r.score, r.sub_scores, r.evidence
    except Exception as exc:  # pragma: no cover - defensive, logged to stderr
        print(f"  scorer {scorer.name} failed on {features.get('tx_hash')}: {exc}",
              file=sys.stderr)
    return -1.0, None, None


def merge_class(sub_scores, evidence, name, score, sub, evd):
    """Apply one re-run class result onto the preserved sub_scores/evidence dicts."""
    if score < 0:
        sub_scores.pop(name, None)
        evidence.pop(name, None)
        return
    if sub is not None:
        sub_scores[name] = sub
    if evd:
        evidence[name] = evd


def recompute_aggregate(scores):
    """(max_score, max_class, risk_band) over the applicable (>=0) class scores."""
    applicable = {k: v for k, v in scores.items() if v >= 0}
    if applicable:
        max_class = max(applicable, key=applicable.get)
        max_score = applicable[max_class]
    else:
        max_class, max_score = "", 0.0
    return round(max_score, 2), max_class, score_to_band(max_score)


def corrected_row(tx_hash, network, scores, sub_scores, evidence, version, prev_at):
    """Build an insert-ready row with the aggregate recomputed and analyzed_at
    bumped +1s so the ReplacingMergeTree supersedes the old row in-partition."""
    max_score, max_class, risk_band = recompute_aggregate(scores)
    return {
        "tx_hash": tx_hash, "network": network, **scores,
        "max_score": max_score, "max_class": max_class, "risk_band": risk_band,
        "sub_scores": sub_scores, "evidence": evidence,
        "analysis_version": version, "analyzed_at": prev_at + timedelta(seconds=1),
    }


def report(corrected, prev_classes):
    """Print the re-score summary (count, max_class churn, new band/class mix)."""
    changed = sum(1 for r, pc in zip(corrected, prev_classes) if r["max_class"] != pc)
    print(f"re-scored {len(corrected)} rows; max_class changed on {changed}")
    print("new risk_band:", dict(Counter(r["risk_band"] for r in corrected)))
    print("new max_class:", dict(Counter(r["max_class"] or "(none)" for r in corrected)))


def write(client, corrected):
    """Insert corrected rows and OPTIMIZE each touched partition so the dedupe
    takes effect immediately rather than waiting for background merges."""
    clickhouse.insert_class_scores(corrected)
    partitions = sorted({r["analyzed_at"].strftime("%Y%m%d") for r in corrected})
    for part in partitions:
        client.execute(f"OPTIMIZE TABLE tx_class_scores PARTITION {part} FINAL")
    print(f"\nInserted {len(corrected)} corrected rows; merged {len(partitions)} partitions.")
