"""Analysis Engine: multi-class orchestrator.

Reads unanalyzed transactions from ClickHouse, runs each enabled scorer's
gate/score pipeline, and writes a 9-element score vector per transaction to
the tx_class_scores table.

The public interface (run_once / run_once_async) is called by tasks/analysis.py.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.db import clickhouse, raw_store
from app.analysis.enrichment import (
    enrich_collision_features as _enrich_collision_features,
    enrich_cycle_features as _enrich_cycle_features,
    enrich_inputs_with_resolved_addresses as _enrich_inputs_with_resolved_addresses,
    enrich_sandwich_features as _enrich_sandwich_features,
    set_main_loop,
)
from app.analysis.normalise import score_to_band
from app.analysis.scorer_config import composite_corroboration_config
from app.analysis.scorers.base import BaseScorer
from app.analysis.scorers.phishing import PhishingScorer
from app.analysis.scorers.token_dust import TokenDustScorer
from app.analysis.scorers.large_value import LargeValueScorer
from app.analysis.scorers.large_datum import LargeDatumScorer
from app.analysis.scorers.multiple_sat import MultipleSatScorer
from app.analysis.scorers.front_running import FrontRunningScorer
from app.analysis.scorers.sandwich import SandwichScorer
from app.analysis.scorers.circular import CircularScorer
from app.analysis.scorers.fake_token import FakeTokenScorer

logger = logging.getLogger(__name__)

# Version tag written to every result row. Bumped to phase5 when the
# fake_token criticality bonus and the cross-class corroboration signal landed,
# so re-scored rows are distinguishable from the prior pass in the
# ReplacingMergeTree history.
_VERSION = "phase5"

# Cross-class corroboration: a class counts as corroborating when it scores at
# or above this threshold. Recorded as a flag only; does not affect max_score
# or risk_band (see config/detection.yaml composite_corroboration).
_CORROBORATION_THRESHOLD = float(
    composite_corroboration_config()["corroboration_threshold"]
)

# All attack class names in canonical order
_CLASS_NAMES = [
    "token_dust", "large_value", "large_datum", "multiple_sat",
    "front_running", "sandwich", "circular", "fake_token", "phishing",
]


def _build_scorers() -> List[BaseScorer]:
    """Instantiate all enabled scorers.

    Scorers for not-yet-implemented classes are simply absent from the list;
    their score defaults to -1 (not applicable) in the output.
    """
    scorers: List[BaseScorer] = []

    if getattr(settings, "SCORER_TOKEN_DUST_ENABLED", True):
        scorers.append(TokenDustScorer())
    if getattr(settings, "SCORER_LARGE_VALUE_ENABLED", True):
        scorers.append(LargeValueScorer())
    if getattr(settings, "SCORER_LARGE_DATUM_ENABLED", True):
        scorers.append(LargeDatumScorer())
    if getattr(settings, "SCORER_MULTIPLE_SAT_ENABLED", True):
        scorers.append(MultipleSatScorer())
    if getattr(settings, "SCORER_FRONT_RUNNING_ENABLED", True):
        scorers.append(FrontRunningScorer())
    if getattr(settings, "SCORER_SANDWICH_ENABLED", True):
        scorers.append(SandwichScorer())
    if getattr(settings, "SCORER_CIRCULAR_ENABLED", True):
        scorers.append(CircularScorer())
    if getattr(settings, "SCORER_FAKE_TOKEN_ENABLED", True):
        scorers.append(FakeTokenScorer())
    if getattr(settings, "SCORER_PHISHING_ENABLED", True):
        scorers.append(PhishingScorer())

    return scorers


def _score_transaction(
    row: Dict[str, Any],
    scorers: List[BaseScorer],
) -> Dict[str, Any]:
    """Run all enabled scorers against a single transaction.

    Returns a dict ready for insert_class_scores().
    """
    tx_hash = row["tx_hash"]
    network = row["network"]
    now = datetime.now(timezone.utc)

    # Both metadata and raw_data are pre-parsed in run_once()
    metadata = row.get("metadata")
    raw_data = row.get("raw_data")

    # Build the features dict available to all scorers
    features: Dict[str, Any] = {
        "tx_hash": tx_hash,
        "network": network,
        "fee": row.get("fee", 0),
        "input_count": row.get("input_count", 0),
        "output_count": row.get("output_count", 0),
        "total_output_value": row.get("total_output_value", 0),
        "metadata": metadata,
        "addresses": row.get("addresses") or [],
        "raw_data": raw_data,
        "slot": row.get("slot"),
        "block_height": row.get("block_height"),
        "timestamp": row.get("timestamp"),
        # Phase 4 cross-tx enrichment data (injected by _enrich_* functions)
        "collision": row.get("collision"),
        "cycle": row.get("cycle"),
        "sandwich": row.get("sandwich"),
    }

    # Run each scorer
    scores: Dict[str, float] = {name: -1.0 for name in _CLASS_NAMES}
    sub_scores: Dict[str, Dict[str, float]] = {}
    evidence: Dict[str, Dict[str, Any]] = {}
    if row.get("raw_data_unavailable"):
        # The raw payload could not be recovered after the fallback budget:
        # raw_data-gated scorers will skip, so mark the degradation in
        # evidence to make it visible and filterable rather than silent.
        evidence["_meta"] = {"raw_data_unavailable": True}

    for scorer in scorers:
        try:
            if scorer.gate(features):
                result = scorer.score(features)
                scores[scorer.name] = result.score
                sub_scores[scorer.name] = result.sub_scores
                if result.evidence:
                    evidence[scorer.name] = result.evidence
        except Exception:
            logger.exception(f"Scorer {scorer.name} failed on tx {tx_hash}")

    # Compute aggregate
    applicable = {k: v for k, v in scores.items() if v >= 0}
    if applicable:
        max_class = max(applicable, key=applicable.get)
        max_score = applicable[max_class]
    else:
        max_class = ""
        max_score = 0.0

    risk_band = score_to_band(max_score)

    # Cross-class corroboration flag. Counts distinct classes scoring at or
    # above the corroboration threshold. Surfaced for analyst filtering only:
    # it deliberately does NOT feed max_score or risk_band, so alerting volume
    # is unchanged.
    corroborating = sorted(k for k, v in applicable.items() if v >= _CORROBORATION_THRESHOLD)

    return {
        "tx_hash": tx_hash,
        "network": network,
        **scores,
        "max_score": round(max_score, 2),
        "max_class": max_class,
        "risk_band": risk_band,
        "corroboration_count": len(corroborating),
        "corroborating_classes": ",".join(corroborating),
        "sub_scores": sub_scores,
        "evidence": evidence,
        "analysis_version": _VERSION,
        "analyzed_at": now,
    }


# Defer bookkeeping for transactions whose raw_data could not be recovered:
# (network, tx_hash) -> (counted attempts, monotonic time of the last counted
# attempt). Attempts are paced by RAW_FALLBACK_RETRY_SECONDS measured on the
# monotonic clock (NTP-step immune):
# the drain loop re-polls every 0.5 s under load, which previously burned
# the whole budget in ~1.5 s and degraded-scored exactly the large
# attack-shaped txs the fallback protects (review finding). In-process only;
# a restart resets the counters, which merely delays degraded scoring.
_raw_fallback_attempts: Dict[tuple, Tuple[int, float]] = {}

# Watermark cursor for the unanalyzed poll: network -> the highest
# ingestion_timestamp scored, minus UNANALYZED_OVERLAP_SECONDS. Bounds all
# three sides of the poll query so its cost tracks the backlog, not the
# total table size. In-process only: a restart starts with a full rescan.
_unanalyzed_watermark: Dict[str, datetime] = {}
# network -> time.monotonic() of the last since=None full rescan.
_last_full_rescan: Dict[str, float] = {}


def _poll_since(network: str) -> Tuple[Optional[datetime], bool]:
    """The ``(since, is_full_rescan)`` bounds for this poll.

    A full rescan (since=None) runs on the first poll after startup and
    then every UNANALYZED_FULL_RESCAN_INTERVAL_SECONDS. It is the
    never-skip guarantee: deferred raw-data txs, input-visibility-deferred
    txs, and anything that slipped past the watermark are picked up within
    one rescan interval.

    Pure: the rescan clock is armed by run_once only AFTER the rescan
    batch succeeds. Arming it here meant a rescan that crashed mid-batch
    (poll error, score-insert failure) was not retried for a whole
    interval, stretching the never-skip guarantee to ~2 intervals.
    """
    now_mono = time.monotonic()
    last = _last_full_rescan.get(network)
    if (
        last is None
        or now_mono - last >= settings.UNANALYZED_FULL_RESCAN_INTERVAL_SECONDS
    ):
        return None, True
    return _unanalyzed_watermark.get(network), False


def _advance_watermark(network: str, rows: List[Dict[str, Any]]) -> None:
    """Advance the poll watermark to the newest fetched row, minus overlap.

    Called only AFTER the batch's score rows are persisted, so a failed
    insert never advances the cursor past unscored work. The overlap absorbs
    same-second ordering skew and the tx-row-before-inputs-row insert gap.
    """
    newest = max(
        (
            r["ingestion_timestamp"] for r in rows
            if isinstance(r.get("ingestion_timestamp"), datetime)
        ),
        default=None,
    )
    if newest is None:
        return
    candidate = newest - timedelta(seconds=settings.UNANALYZED_OVERLAP_SECONDS)
    current = _unanalyzed_watermark.get(network)
    if current is None or candidate > current:
        _unanalyzed_watermark[network] = candidate


def _resolve_raw_data(
    rows: List[Dict[str, Any]], network: str,
) -> List[Dict[str, Any]]:
    """Parse each row's raw_data, recovering from the raw store when needed.

    The stored column may be empty-with-flag (payload over RAW_DATA_MAX_BYTES)
    or unparseable (legacy rows written with the old mid-JSON truncation).
    Previously such rows were scored with raw_data=None: every raw_data-gated
    scorer silently skipped, the tx was written all -1, and it was NEVER
    re-evaluated — exactly the large, attack-shaped transactions. Now:

      1. Recover the full payload from the raw store (read_confirmed probes
         the day directories derived from the row's timestamp).
      2. On a failed read, DEFER the tx (drop it from this batch, no score
         row written) so the next engine poll retries, up to
         RAW_FALLBACK_MAX_ATTEMPTS.
      3. After the attempt budget, score degraded with raw_data=None and a
         raw_data_unavailable evidence marker, so a lost blob cannot park
         the tx in the unanalyzed queue forever and the degradation is
         visible/filterable instead of silent.

    Returns the rows to score this run (deferred rows removed).
    """
    kept: List[Dict[str, Any]] = []
    for row in rows:
        rd = row.get("raw_data")
        truncated = bool(row.get("raw_data_truncated"))
        parsed: Optional[Dict[str, Any]] = None
        if isinstance(rd, dict):
            parsed = rd
        elif isinstance(rd, str) and rd:
            try:
                parsed = json.loads(rd)
            except (json.JSONDecodeError, TypeError):
                parsed = None

        needs_recovery = parsed is None and (
            truncated or (isinstance(rd, str) and bool(rd))
        )
        if needs_recovery and settings.RAW_FALLBACK_ENABLED:
            ts = row.get("timestamp")
            if isinstance(ts, datetime):
                try:
                    parsed = raw_store.read_confirmed(network, row["tx_hash"], ts)
                except Exception:
                    logger.exception(
                        "Raw store fallback failed for %s", row["tx_hash"][:16],
                    )
                    parsed = None

        if needs_recovery and parsed is None:
            key = (network, row["tx_hash"])
            entry = _raw_fallback_attempts.get(key)
            now_mono = time.monotonic()
            if (
                entry is not None
                and now_mono - entry[1] < settings.RAW_FALLBACK_RETRY_SECONDS
            ):
                # Re-polled inside the pacing window: defer WITHOUT counting,
                # or a busy drain loop exhausts the budget in seconds.
                logger.debug(
                    "Deferring %s: raw_data unrecoverable (attempt window)",
                    row["tx_hash"][:16],
                )
                continue
            attempts = (entry[0] if entry else 0) + 1
            if attempts < settings.RAW_FALLBACK_MAX_ATTEMPTS:
                _raw_fallback_attempts[key] = (attempts, now_mono)
                logger.warning(
                    "Deferring %s: raw_data unrecoverable (attempt %d/%d)",
                    row["tx_hash"][:16], attempts,
                    settings.RAW_FALLBACK_MAX_ATTEMPTS,
                )
                continue
            _raw_fallback_attempts.pop(key, None)
            row["raw_data_unavailable"] = True
            logger.error(
                "Scoring %s degraded: raw_data unrecoverable after %d attempts",
                row["tx_hash"][:16], attempts,
            )
        elif needs_recovery:
            _raw_fallback_attempts.pop((network, row["tx_hash"]), None)

        row["raw_data"] = parsed
        kept.append(row)
    return kept


def run_once(network: str) -> int:
    """Score one batch of unanalyzed transactions.  Returns the count scored."""
    if not settings.ANALYSIS_ENABLED:
        return 0

    since, full_rescan = _poll_since(network)
    fetched = clickhouse.get_unanalyzed_transactions(
        network, settings.ANALYSIS_ENGINE_BATCH_SIZE, since=since,
    )
    if not fetched:
        if full_rescan:
            # An empty fetch IS a successful rescan: nothing was skipped.
            _last_full_rescan[network] = time.monotonic()
        return 0
    rows = fetched

    scorers = _build_scorers()

    # Parse raw_data (with raw-store recovery / deferral) and metadata so
    # scorers receive dicts, not strings.
    rows = _resolve_raw_data(rows, network)
    if not rows:
        if full_rescan:
            # Every row deferred is still a SUCCESSFUL rescan (the poll ran;
            # deferral is deliberate, paced bookkeeping) — re-running the
            # full scan every drain tick would hammer the warehouse.
            _last_full_rescan[network] = time.monotonic()
        return 0
    for row in rows:
        md = row.get("metadata")
        if isinstance(md, str) and md not in ("", "{}"):
            try:
                row["metadata"] = json.loads(md)
            except (json.JSONDecodeError, TypeError):
                row["metadata"] = None
        elif not md or md == "{}":
            row["metadata"] = None

    # Enrich inputs with resolved addresses from transaction_inputs table
    _enrich_inputs_with_resolved_addresses(rows, network)

    # Enrich collision features for front-running detection
    _enrich_collision_features(rows, network)

    # Enrich cycle features for circular transfer detection
    _enrich_cycle_features(rows, network)

    # Enrich sandwich features for structural sandwich detection
    _enrich_sandwich_features(rows, network)

    results = []
    for row in rows:
        result = _score_transaction(row, scorers)
        results.append(result)

    clickhouse.insert_class_scores(results)
    # Only after the score rows are durably written: a failed insert raises
    # above and the cursor stays put, so the batch is re-polled. Advanced
    # over ALL fetched rows (including raw-data-deferred ones, which the
    # periodic full rescan recovers).
    _advance_watermark(network, fetched)
    if full_rescan:
        # Armed only now: a rescan that crashed above re-runs on the next
        # poll instead of waiting out a full interval (never-skip guarantee).
        _last_full_rescan[network] = time.monotonic()

    # Log summary
    critical = sum(1 for r in results if r["risk_band"] == "Critical")
    high = sum(1 for r in results if r["risk_band"] == "High")
    scored_classes = {}
    for r in results:
        for cls in _CLASS_NAMES:
            if r.get(cls, -1) >= 0:
                scored_classes[cls] = scored_classes.get(cls, 0) + 1

    class_summary = ", ".join(f"{k}={v}" for k, v in scored_classes.items()) or "none"
    logger.info(
        f"Analysis Engine [{network}]: scored {len(results)} txs "
        f"(Critical={critical}, High={high}) classes: {class_summary}"
    )
    # Return the FETCHED count, not the scored count: raw-data deferrals can
    # shrink the scored set, and the drain loop keys "queue still has work"
    # off whether the poll filled the batch.
    return len(fetched)


async def run_once_async(network: str) -> int:
    """Non-blocking wrapper: runs run_once on the dedicated ClickHouse executor."""
    loop = asyncio.get_running_loop()
    # Collision enrichment runs on a worker thread and bridges async postgres
    # calls back onto this loop (see app.analysis.enrichment).
    set_main_loop(loop)
    return await loop.run_in_executor(clickhouse._ch_executor, run_once, network)
