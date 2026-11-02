"""Analysis Engine: multi-class orchestrator.

Reads unanalyzed transactions from ClickHouse, runs each enabled scorer's
gate/score pipeline, and writes a 9-element score vector per transaction to
the tx_class_scores table.

Two lanes poll for work. The live lane (run_once) scores the newest
transactions of a short window of ingestion time; the backlog lane
(run_backlog_once) scores, oldest first, whatever left that window unscored.
Both are driven by tasks/analysis.py, each on its own executor thread.
"""

import asyncio
import functools
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from app import notifications
from app.analysis import graph as graph_mod
from app.analysis.contract_identity import contract_address_of
from app.analysis.enrichment import (
    enrich_collision_features as _enrich_collision_features,
)
from app.analysis.enrichment import (
    enrich_cycle_features as _enrich_cycle_features,
)
from app.analysis.enrichment import (
    enrich_inputs_with_resolved_addresses as _enrich_inputs_with_resolved_addresses,
)
from app.analysis.enrichment import (
    enrich_sandwich_features as _enrich_sandwich_features,
)
from app.analysis.enrichment import (
    set_main_loop,
)
from app.analysis.normalise import score_to_band
from app.analysis.scorer_config import (
    BASELINE_EVIDENCE_KEY,
    composite_corroboration_config,
    config_hash,
    record_baselines,
)
from app.analysis.scorers.base import BaseScorer
from app.analysis.scorers.circular import CircularScorer
from app.analysis.scorers.fake_token import FakeTokenScorer
from app.analysis.scorers.front_running import FrontRunningScorer
from app.analysis.scorers.large_datum import LargeDatumScorer
from app.analysis.scorers.large_value import LargeValueScorer
from app.analysis.scorers.multiple_sat import MultipleSatScorer
from app.analysis.scorers.phishing import PhishingScorer
from app.analysis.scorers.sandwich import SandwichScorer
from app.analysis.scorers.token_dust import TokenDustScorer
from app.config import settings
from app.db import clickhouse, raw_store
from app.utils.datetime_utils import to_aware_utc

logger = logging.getLogger(__name__)

# Version tag written to every result row. Bumped to phase5 when the
# fake_token criticality bonus and the cross-class corroboration signal landed,
# so re-scored rows are distinguishable from the prior pass in the
# ReplacingMergeTree history.
_VERSION = "phase5"

# Cross-class corroboration: a class counts as corroborating when it scores at
# or above this threshold. Recorded as a flag only; does not affect max_score
# or risk_band (see config/detection.yaml composite_corroboration).
_CORROBORATION_THRESHOLD = float(composite_corroboration_config()["corroboration_threshold"])

# All attack class names in canonical order
_CLASS_NAMES = [
    "token_dust",
    "large_value",
    "large_datum",
    "multiple_sat",
    "front_running",
    "sandwich",
    "circular",
    "fake_token",
    "phishing",
]


def _build_scorers() -> list[BaseScorer]:
    """Instantiate all enabled scorers.

    Scorers for not-yet-implemented classes are simply absent from the list;
    their score defaults to -1 (not applicable) in the output.
    """
    scorers: list[BaseScorer] = []

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
    row: dict[str, Any],
    scorers: list[BaseScorer],
) -> dict[str, Any]:
    """Run all enabled scorers against a single transaction.

    Returns a dict ready for insert_class_scores().
    """
    tx_hash = row["tx_hash"]
    network = row["network"]
    now = datetime.now(UTC)

    # Both metadata and raw_data are pre-parsed in run_once()
    metadata = row.get("metadata")
    raw_data = row.get("raw_data")

    # Build the features dict available to all scorers
    features: dict[str, Any] = {
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
    scores: dict[str, float] = {name: -1.0 for name in _CLASS_NAMES}
    sub_scores: dict[str, dict[str, float]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    # Which baseline tier each scorer used (per_script / per_policy / global /
    # fixed / missing). Kept so the notification payload can report
    # the winning class's baseline_source; not persisted to ClickHouse. The
    # per-axis detail, including the (p50, p99) each axis was actually measured
    # against, is recorded into each class's evidence instead (see
    # BASELINE_EVIDENCE_KEY): `baselines` is maintained in place, so those
    # values are unrecoverable afterwards unless the score carries them.
    baseline_sources: dict[str, str] = {}
    if row.get("raw_data_unavailable"):
        # The raw payload could not be recovered after the fallback budget:
        # raw_data-gated scorers will skip, so mark the degradation in
        # evidence to make it visible and filterable rather than silent.
        evidence["_meta"] = {"raw_data_unavailable": True}

    # Scorers that RAISED (not merely gated out). A raised scorer leaves its
    # class at the -1 sentinel, indistinguishable from "not applicable"; the
    # engine uses this list to defer the tx (retry) rather than persist a row
    # that silently masks the unscored class. See _handle_incomplete_scoring.
    failed_scorers: list[str] = []
    for scorer in scorers:
        try:
            if scorer.gate(features):
                with record_baselines() as baselines_used:
                    result = scorer.score(features)
                scores[scorer.name] = result.score
                sub_scores[scorer.name] = result.sub_scores
                baseline_sources[scorer.name] = result.baseline_source
                if result.evidence or baselines_used:
                    # Copied, not mutated: the ScorerResult belongs to the
                    # scorer, and the resolved baselines are the engine's
                    # addition to what the scorer chose to report.
                    class_evidence = dict(result.evidence or {})
                    if baselines_used:
                        class_evidence[BASELINE_EVIDENCE_KEY] = baselines_used
                    evidence[scorer.name] = class_evidence
        except Exception:
            logger.exception(f"Scorer {scorer.name} failed on tx {tx_hash}")
            failed_scorers.append(scorer.name)

    # Compute aggregate
    applicable = {k: v for k, v in scores.items() if v >= 0}
    if applicable:
        max_class = max(applicable, key=lambda k: applicable[k])
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
        "baseline_source": baseline_sources.get(max_class, "missing"),
        "corroboration_count": len(corroborating),
        "corroborating_classes": ",".join(corroborating),
        # Normalized from the winning class's own evidence key so alerts can be
        # grouped by contract without a reader knowing which scorer won. '' when
        # the class names no contract (see contract_identity.NO_CONTRACT).
        "contract_address": contract_address_of(max_class, evidence),
        "sub_scores": sub_scores,
        "evidence": evidence,
        "analysis_version": _VERSION,
        # Provenance, so a stored verdict can be re-derived rather than only
        # explained. `analysis_version` names the scoring generation and moves
        # rarely; `config_hash` moves whenever any weight, threshold, band cut
        # or allowlist in detection.yaml changes, and `code_version` names the
        # build. Together they name the tuning and the build behind this number;
        # the learned percentiles it was normalised against are recorded
        # separately, in each class's evidence.
        "config_hash": config_hash(),
        "code_version": settings.CODE_VERSION,
        "analyzed_at": now,
        # Transient keys (popped by _handle_incomplete_scoring before insert):
        # which scorers raised and which enrichments failed for this tx, so an
        # incompletely-scored tx is deferred/retried instead of silently kept.
        "_failed_scorers": failed_scorers,
        "_enrichment_failed": list(row.get("_enrichment_failed") or []),
    }


# Defer bookkeeping for transactions whose raw_data could not be recovered:
# (network, tx_hash) -> (counted attempts, monotonic time of the last counted
# attempt). Attempts are paced by RAW_FALLBACK_RETRY_SECONDS measured on the
# monotonic clock (NTP-step immune):
# the drain loop re-polls every 0.5 s under load, which previously burned
# the whole budget in ~1.5 s and degraded-scored exactly the large
# attack-shaped txs the fallback protects (review finding). In-process only;
# a restart resets the counters, which merely delays degraded scoring.
_raw_fallback_attempts: dict[tuple, tuple[int, float]] = {}

# Transactions a lane has polled and not finished, as (network, tx_hash). The
# lanes run on different threads, and a transaction that ages out of the live
# window while the live lane is scoring it sits, unscored, in the backlog
# lane's range for that moment: the claim keeps it from being scored twice.
_in_flight: set[tuple[str, str]] = set()
_in_flight_lock = threading.Lock()


def _claim(network: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Take the rows no other lane is scoring. The caller must _release them."""
    with _in_flight_lock:
        mine = [r for r in rows if (network, r["tx_hash"]) not in _in_flight]
        _in_flight.update((network, r["tx_hash"]) for r in mine)
    return mine


def _release(network: str, rows: list[dict[str, Any]]) -> None:
    with _in_flight_lock:
        _in_flight.difference_update((network, r["tx_hash"]) for r in rows)


def _resolve_raw_data(
    rows: list[dict[str, Any]],
    network: str,
) -> list[dict[str, Any]]:
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
    kept: list[dict[str, Any]] = []
    for row in rows:
        rd = row.get("raw_data")
        truncated = bool(row.get("raw_data_truncated"))
        parsed: dict[str, Any] | None = None
        if isinstance(rd, dict):
            parsed = rd
        elif isinstance(rd, str) and rd:
            try:
                parsed = json.loads(rd)
            except (json.JSONDecodeError, TypeError):
                parsed = None

        needs_recovery = parsed is None and (truncated or (isinstance(rd, str) and bool(rd)))
        if needs_recovery and settings.RAW_FALLBACK_ENABLED:
            ts = row.get("timestamp")
            if isinstance(ts, datetime):
                try:
                    parsed = raw_store.read_confirmed(network, row["tx_hash"], ts)
                except Exception:
                    logger.exception(
                        "Raw store fallback failed for %s",
                        row["tx_hash"][:16],
                    )
                    parsed = None

        if needs_recovery and parsed is None:
            key = (network, row["tx_hash"])
            entry = _raw_fallback_attempts.get(key)
            now_mono = time.monotonic()
            if entry is not None and now_mono - entry[1] < settings.RAW_FALLBACK_RETRY_SECONDS:
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
                    row["tx_hash"][:16],
                    attempts,
                    settings.RAW_FALLBACK_MAX_ATTEMPTS,
                )
                continue
            _raw_fallback_attempts.pop(key, None)
            row["raw_data_unavailable"] = True
            logger.error(
                "Scoring %s degraded: raw_data unrecoverable after %d attempts",
                row["tx_hash"][:16],
                attempts,
            )
        elif needs_recovery:
            _raw_fallback_attempts.pop((network, row["tx_hash"]), None)

        row["raw_data"] = parsed
        kept.append(row)
    return kept


# Defer bookkeeping for transactions whose scoring was INCOMPLETE (a scorer
# raised, or a cross-tx enrichment failed): (network, tx_hash) -> (counted
# attempts, monotonic time of the last counted attempt). Same pacing/budget
# discipline and in-process-only caveat as _raw_fallback_attempts above.
_analysis_defer_attempts: dict[tuple, tuple[int, float]] = {}


def _handle_incomplete_scoring(
    results: list[dict[str, Any]],
    network: str,
) -> list[dict[str, Any]]:
    """Defer transactions whose scoring was incomplete; degrade after budget.

    A result is "incomplete" when a scorer raised (``_failed_scorers``) or a
    required enrichment failed (``_enrichment_failed``). Writing such a row
    would leave the affected class at the -1 sentinel, and the unanalyzed
    anti-join (clickhouse_scores.get_unanalyzed_transactions) treats ANY
    written row as scored, so the tx would never be re-evaluated -- a silent,
    permanent recall hole. Instead:

      1. Defer the tx (drop it from this batch, no score row written) so the
         next poll of its lane retries it, up to
         ANALYSIS_DEFER_MAX_ATTEMPTS, paced by ANALYSIS_DEFER_RETRY_SECONDS.
      2. After the budget, keep the tx but attach an evidence ``_meta`` marker
         (``scorer_failed`` / ``enrichment_unavailable``) so a deterministic
         crash or a persistent outage cannot park the tx forever and the
         degradation is queryable/filterable instead of a silent -1.

    Mirrors _resolve_raw_data's recover -> defer -> degrade-with-marker shape.
    Always pops the two transient keys so the result is clean for insert.
    """
    if not settings.ANALYSIS_DEFER_ENABLED:
        for r in results:
            r.pop("_failed_scorers", None)
            r.pop("_enrichment_failed", None)
        return results

    kept: list[dict[str, Any]] = []
    now_mono = time.monotonic()
    for r in results:
        failed_scorers = r.pop("_failed_scorers", None) or []
        failed_enrichment = r.pop("_enrichment_failed", None) or []
        key = (network, r["tx_hash"])
        if not failed_scorers and not failed_enrichment:
            # Complete: clear any stale defer bookkeeping and keep.
            _analysis_defer_attempts.pop(key, None)
            kept.append(r)
            continue

        entry = _analysis_defer_attempts.get(key)
        if entry is not None and now_mono - entry[1] < settings.ANALYSIS_DEFER_RETRY_SECONDS:
            # Re-polled inside the pacing window: defer WITHOUT counting.
            logger.debug(
                "Deferring %s: incomplete scoring (attempt window)",
                r["tx_hash"][:16],
            )
            continue
        attempts = (entry[0] if entry else 0) + 1
        if attempts < settings.ANALYSIS_DEFER_MAX_ATTEMPTS:
            _analysis_defer_attempts[key] = (attempts, now_mono)
            logger.warning(
                "Deferring %s: incomplete scoring (scorers=%s enrichment=%s) attempt %d/%d",
                r["tx_hash"][:16],
                failed_scorers,
                failed_enrichment,
                attempts,
                settings.ANALYSIS_DEFER_MAX_ATTEMPTS,
            )
            continue
        # Budget exhausted: keep the row but mark the degradation so it is
        # visible/filterable rather than a silent -1.
        _analysis_defer_attempts.pop(key, None)
        meta = r.setdefault("evidence", {}).setdefault("_meta", {})
        if failed_scorers:
            meta["scorer_failed"] = failed_scorers
        if failed_enrichment:
            meta["enrichment_unavailable"] = failed_enrichment
        logger.error(
            "Scoring %s degraded: incomplete after %d attempts (scorers=%s enrichment=%s)",
            r["tx_hash"][:16],
            attempts,
            failed_scorers,
            failed_enrichment,
        )
        kept.append(r)
    return kept


class BacklogBatch(NamedTuple):
    """What scoring one backlog batch did."""

    # Picked transactions still there to score (a rollback purge can remove
    # one between the pick and the batch).
    found: int
    # How many of those now have a score. Fewer means the rest wait on a retry
    # or on the live lane, so picking their slice again at once gains nothing.
    scored: int


def run_once(
    network: str,
    history_complete: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Live lane: score the newest unscored transactions of the live window.

    Returns how many the poll returned, which the drain loop compares with the
    batch size. ``history_complete`` is False while the backlog lane still
    holds older unscored transactions: features read from earlier scores
    (circular recurrence and recycling) may then be missing some, and each
    score written meanwhile says so (``_meta.history_incomplete``).

    The poll passes over transactions waiting out a deferral's pacing: re-polled
    they would defer again uncounted, and being the newest unscored they would
    be all a poll returned, so the drain would never reach older ones.
    """
    if not settings.ANALYSIS_ENABLED:
        return 0
    since = datetime.now(UTC) - timedelta(seconds=settings.ANALYSIS_LIVE_WINDOW_SECONDS)
    fetched = clickhouse.get_unanalyzed_transactions(
        network,
        settings.ANALYSIS_ENGINE_BATCH_SIZE,
        since=since,
        newest_first=True,
        exclude=_paced(network),
    )
    _score_batch(network, fetched, "live", history_complete, should_stop)
    return len(fetched)


def _paced(network: str) -> set[str]:
    """Deferred transactions of ``network`` whose next counted attempt is not due yet."""
    now_mono = time.monotonic()
    paced = set()
    for attempts, retry_seconds in (
        (_raw_fallback_attempts, settings.RAW_FALLBACK_RETRY_SECONDS),
        (_analysis_defer_attempts, settings.ANALYSIS_DEFER_RETRY_SECONDS),
    ):
        # list(): the other lane's thread may add an entry meanwhile.
        for (net, tx_hash), (_, last) in list(attempts.items()):
            if net == network and now_mono - last < retry_seconds:
                paced.add(tx_hash)
    return paced


def pick_backlog(network: str, since: datetime, until: datetime) -> list[tuple[str, datetime]]:
    """Backlog lane: (tx_hash, ingestion stamp) of the oldest unscored in [since, until).

    Up to ANALYSIS_BACKLOG_PICK_SIZE, scored afterwards a batch at a time
    (run_backlog_once): a pick reaching merged parts has a fixed cost (see
    clickhouse_scores.pick_unanalyzed), which one pick per batch would pay
    tens of thousands of times over a large backlog.
    """
    picked = clickhouse.pick_unanalyzed(
        network, settings.ANALYSIS_BACKLOG_PICK_SIZE, since=since, until=until
    )
    return [(tx_hash, to_aware_utc(ingested)) for tx_hash, ingested in picked]


def run_backlog_once(
    network: str,
    hashes: list[str],
    since: datetime,
    until: datetime,
    should_stop: Callable[[], bool] | None = None,
) -> BacklogBatch:
    """Backlog lane: score picked transactions, ingested in [since, until).

    Every score it writes is late by construction (the transaction left the
    live window unscored), and its alert says so.
    """
    if not settings.ANALYSIS_ENABLED:
        return BacklogBatch(0, 0)
    rows = clickhouse.hydrate_transactions(network, hashes, since=since, until=until)
    return BacklogBatch(len(rows), _score_batch(network, rows, "backlog", True, should_stop))


def _score_batch(
    network: str,
    fetched: list[dict[str, Any]],
    lane: str,
    history_complete: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Score and persist one polled batch, minus what the other lane holds.

    Returns how many score rows it wrote. Once ``should_stop`` reads True (its
    lane was stopped while the batch ran) it stores nothing: see _score_claimed.
    """
    rows = _claim(network, fetched)
    try:
        stored = _score_claimed(network, rows, lane, history_complete, should_stop=should_stop)
    finally:
        _release(network, rows)
    _rescore_later_passes(network, stored, lane, history_complete, should_stop)
    return len(stored)


def _raises_circular(before: dict[str, graph_mod.StoredCycle], result: dict[str, Any]) -> bool:
    """Whether a re-score is kept: circular up, and the headline score not down.

    Scored again late, a pass's forward search can take in its successors'
    legs and read lower (graph.py, hop amounts), so a re-score that would
    lower the stored score is dropped and the stored score stands.
    """
    stored = before[result["tx_hash"]]
    return result.get("circular", -1) > stored.circular and result["max_score"] >= stored.max_score


def _rescore_later_passes(
    network: str,
    stored: list[dict[str, Any]],
    lane: str,
    history_complete: bool,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Re-score the origin's later passes that were stored before these.

    A cycle's recycling and recurrence axes read its origin's earlier cycles
    from the stored scores, but neither lane stores in chain order: the live
    lane works newest first and the backlog lane stores what the live lane
    left after newer passes. Without this a later pass stored first would
    keep its lower score for good. A kept re-score is stored like any score
    (and alerts if its band rose), so the passes after it are looked up in
    turn; each round can only raise scores, so the cascade ends. The number
    re-scored is capped by ANALYSIS_HISTORY_RESCORE_MAX_TXS.
    """
    budget = settings.ANALYSIS_HISTORY_RESCORE_MAX_TXS
    while stored and budget > 0:
        sources = [
            (r["tx_hash"], r.get("circular", -1), r["evidence"]["circular"])
            for r in stored
            if isinstance(r.get("evidence", {}).get("circular"), dict)
        ]
        if not sources:
            return
        try:
            later = graph_mod.stored_later_cycles(network, sources)
            if not later:
                return
            # Chain order, so that a cascade reaches each pass after the ones before it.
            targets = sorted(later, key=lambda h: later[h].first_slot)
            if len(targets) > budget:
                logger.error(
                    "Circular history re-score [%s]: %d later passes, re-scoring the first %d; "
                    "the rest keep their stored score (ANALYSIS_HISTORY_RESCORE_MAX_TXS)",
                    network,
                    len(targets),
                    budget,
                )
                targets = targets[:budget]
            budget -= len(targets)
            rows = _claim(network, clickhouse.hydrate_transactions(network, targets, since=None))
            try:
                stored = _score_claimed(
                    network,
                    rows,
                    lane,
                    history_complete,
                    keep=functools.partial(_raises_circular, later),
                    should_stop=should_stop,
                )
            finally:
                _release(network, rows)
        except Exception:
            # The batch that triggered it is stored; only the raised scores are
            # lost, so this must not fail the batch.
            logger.error(
                "Circular history re-score failed for %s; the later passes keep their stored score",
                network,
                exc_info=True,
            )
            return


def _score_claimed(
    network: str,
    rows: list[dict[str, Any]],
    lane: str,
    history_complete: bool,
    keep: Callable[[dict[str, Any]], bool] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Score, persist and alert ``rows``; returns the results it stored.

    ``keep``, when given, drops results before they are stored (a re-score
    that would not raise the stored one). Nothing is stored once
    ``should_stop`` reads True: a batch that outlives its lane's stop at
    shutdown may have run its enrichments after the event loop stopped, and
    its alerts could not be sent. Left unscored, its rows are picked again
    after the restart (the checkpoint has not passed them).
    """
    if not rows:
        return []

    scorers = _build_scorers()

    # Parse raw_data (with raw-store recovery / deferral) and metadata so
    # scorers receive dicts, not strings. A deferred row stays unscored, so
    # its lane polls it again: the live lane while it is in the window, then
    # the backlog lane, whose checkpoint cannot pass it until it is scored.
    rows = _resolve_raw_data(rows, network)
    if not rows:
        return []
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

    late = lane == "backlog"
    results = []
    for row in rows:
        result = _score_transaction(row, scorers)
        # Read by the alert payload only; insert_class_scores ignores them.
        result["tx_timestamp"] = row.get("timestamp")
        result["late_detection"] = late
        if not history_complete:
            result["evidence"].setdefault("_meta", {})["history_incomplete"] = True
        results.append(result)

    # Defer txs whose scoring was incomplete (a scorer raised, or an enrichment
    # failed) so they are retried instead of persisted with a silent -1; after
    # the retry budget they are kept with a visible degradation marker.
    results = _handle_incomplete_scoring(results, network)
    if keep is not None:
        results = [r for r in results if keep(r)]
    if not results:
        return []
    if should_stop is not None and should_stop():
        logger.info(
            "Analysis Engine [%s] %s lane stopped: %d scored txs left unstored, picked "
            "again after the restart",
            network,
            lane,
            len(results),
        )
        return []

    clickhouse.insert_class_scores(results)
    # Emit immediate alerts for the just-persisted scores
    # Fire-and-forget onto the main loop; never blocks this scoring thread.
    notifications.on_new_scores(results, network)

    # Log summary
    critical = sum(1 for r in results if r["risk_band"] == "Critical")
    high = sum(1 for r in results if r["risk_band"] == "High")
    scored_classes: dict[str, int] = {}
    for r in results:
        for cls in _CLASS_NAMES:
            if r.get(cls, -1) >= 0:
                scored_classes[cls] = scored_classes.get(cls, 0) + 1

    class_summary = ", ".join(f"{k}={v}" for k, v in scored_classes.items()) or "none"
    logger.info(
        f"Analysis Engine [{network}] {lane} lane: scored {len(results)} txs "
        f"(Critical={critical}, High={high}) classes: {class_summary}"
    )
    return results


async def run_once_async(
    network: str,
    history_complete: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Non-blocking wrapper: runs run_once on the live lane's own executor."""
    loop = asyncio.get_running_loop()
    # Collision enrichment runs on a worker thread and bridges async postgres
    # calls back onto this loop (see app.analysis.enrichment).
    set_main_loop(loop)
    return await loop.run_in_executor(
        clickhouse._ch_live_executor, run_once, network, history_complete, should_stop
    )


async def pick_backlog_async(
    network: str, since: datetime, until: datetime
) -> list[tuple[str, datetime]]:
    """Non-blocking wrapper: runs pick_backlog on the backlog executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        clickhouse._ch_backlog_executor, pick_backlog, network, since, until
    )


async def run_backlog_once_async(
    network: str,
    hashes: list[str],
    since: datetime,
    until: datetime,
    should_stop: Callable[[], bool] | None = None,
) -> BacklogBatch:
    """Non-blocking wrapper: runs run_backlog_once on the backlog executor.

    Sets the main loop too: without it collision enrichment is skipped, not
    failed, and front_running would score the backlog as no-signal.
    """
    loop = asyncio.get_running_loop()
    set_main_loop(loop)
    return await loop.run_in_executor(
        clickhouse._ch_backlog_executor,
        run_backlog_once,
        network,
        hashes,
        since,
        until,
        should_stop,
    )
