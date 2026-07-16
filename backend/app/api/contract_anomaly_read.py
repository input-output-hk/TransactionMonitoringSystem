"""Read-time projection of the clustering sidecar's verdicts onto the analysis API.

The synthetic ``contract_anomaly`` attack class has no stored ``tx_class_scores``
column: it is computed entirely on READ from the optional clustering sidecar's
raw verdicts (see :mod:`app.analysis.contract_anomaly` for the per-verdict score
projection). This module holds that read-time OVERLAY, kept separate from
:mod:`app.api.analysis` so the generic 9-class API endpoints stay readable:

- hydration of a ClickHouse score row into a :class:`ClassScoreResult`
  (``_row_to_class_score``), shared by every read path;
- the additive per-tx merge of a sidecar verdict (``_merge_contract_anomaly``)
  and its page-level batch form (``_merge_overlay_onto_page``);
- the recall rescue that re-admits flagged txs a stored-score filter dropped
  (``_rescue_flagged_onto_page``) and the dedicated ``attack_class=contract_anomaly``
  list page (``_list_contract_anomaly_results``);
- reconciliation of the stats / timeseries aggregates to the EFFECTIVE band
  (``_augment_stats_with_contract_anomaly`` / ``_augment_timeseries_with_contract_anomaly``).

All of it is recall-first (see CLAUDE.md): the merge only ever RAISES a score /
band and never mutates the stored per-tx fields. The dependency is one-way
(:mod:`app.api.analysis` imports from here, never the reverse).
"""

import json
import logging
from datetime import datetime
from typing import Any

from app.analysis import contract_anomaly as ca_projection
from app.analysis.contract_anomaly import corroboration_threshold
from app.analysis.engine import _CLASS_NAMES
from app.analysis.normalise import score_to_band
from app.config import settings
from app.db import clickhouse, clustering_queries
from app.models.transaction import ALERT_BANDS, ClassScoreResult, RiskBand
from app.utils.datetime_utils import to_aware_utc

logger = logging.getLogger(__name__)

# The synthetic class merged in at read time from the clustering sidecar. It is
# NOT in _CLASS_NAMES (which mirrors the nine hardcoded tx_class_scores columns)
# so the per-tx write path stays untouched; it is injected after hydration.
_CONTRACT_ANOMALY = "contract_anomaly"


def _sort_results(results: list[ClassScoreResult], *, by_date: bool) -> None:
    """Re-rank a hydrated result list in place to mirror the SQL ORDER BY.

    ``by_date`` sorts (analyzed_at, max_score) descending; otherwise
    (max_score, analyzed_at) descending. Shared by the contract_anomaly list
    filter and the recall rescue so the two read paths order identically.
    ``analyzed_at`` is a required datetime on :class:`ClassScoreResult`, so the
    key never mixes None with datetime."""
    if by_date:
        results.sort(key=lambda d: (d.analyzed_at, d.max_score), reverse=True)
    else:
        results.sort(key=lambda d: (d.max_score, d.analyzed_at), reverse=True)


def _merge_contract_anomaly(
    result: ClassScoreResult,
    rows: list[dict[str, Any]],
) -> None:
    """Fold the clustering sidecar's verdict(s) for a tx into a hydrated result.

    ``rows`` are the raw per-(watched-contract) verdict rows; this resolves them
    to the highest-severity one (host-scale score computed from the projection
    config) and merges it additively. Recall-first: it only ever RAISES
    max_score / risk_band via max(...); it never lowers an existing class score
    and never mutates the stored, server-filterable corroboration_count (the
    contract_anomaly corroboration signal rides on its own boolean field).
    Mutates ``result`` in place; a no-op when ``rows`` is empty.
    """
    resolved = ca_projection.resolve(rows)
    if resolved is None:
        return
    score = float(resolved["score"])
    result.scores[_CONTRACT_ANOMALY] = score
    result.sub_scores[_CONTRACT_ANOMALY] = {
        "consensus": float(resolved.get("consensus") or 0.0),
        "votes": int(resolved.get("votes", 0) or 0),
        "cluster_id": int(resolved.get("cluster_id", -1)),
        "verdict": resolved.get("verdict", ""),
    }
    evidence = resolved.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    result.evidence[_CONTRACT_ANOMALY] = {
        **evidence,
        "target": resolved.get("target", ""),
        "model_id": resolved.get("model_id", ""),
        "feature_set": resolved.get("feature_set", ""),
    }
    if score > result.max_score:
        result.max_score = score
        result.max_class = _CONTRACT_ANOMALY
        result.risk_band = RiskBand(score_to_band(score))
    result.contract_anomaly_corroborates = score >= corroboration_threshold()
    result.contract_anomaly_scored_at = resolved.get("scored_at")


def _passes_score_band(
    score: float,
    band: RiskBand,
    min_score: float,
    bands: list[str] | None,
) -> bool:
    """Whether a (score, band) pair satisfies the list view's score/band filter.

    Mirrors the DB-side predicate in ``_score_filter_conditions`` (max_score >=
    min_score AND lower(risk_band) IN bands) so the contract_anomaly rescue
    admits exactly the rows the DB filter would have, had it seen the merged
    score. Empty/None ``bands`` means no band restriction."""
    if min_score > 0 and score < min_score:
        return False
    if bands and band.value.lower() not in {b.lower() for b in bands}:
        return False
    return True


def _within_analyzed_window(
    analyzed_at: Any,
    analyzed_from: datetime | None,
    analyzed_to: datetime | None,
) -> bool:
    """Mirror the DB analyzed_at bounds (>= from, < to) for a rescued row.

    The stored-class path filters analyzed_at IN THE DB, so it never compares
    datetimes in Python. This synthetic class filters in Python instead, mixing
    ClickHouse's naive-UTC ``analyzed_at`` with the API's ``analyzed_from`` /
    ``analyzed_to`` (the frontend sends ``...Z``, so FastAPI parses them
    tz-AWARE). :func:`to_aware_utc` normalises both sides so the compare can't
    raise the naive-vs-aware TypeError (which the endpoint would swallow into an
    empty page)."""
    if analyzed_at is None:
        return analyzed_from is None and analyzed_to is None
    at = to_aware_utc(analyzed_at)
    lo = to_aware_utc(analyzed_from)
    hi = to_aware_utc(analyzed_to)
    if lo is not None and at < lo:
        return False
    if hi is not None and at >= hi:
        return False
    return True


# Band severity ordering for the effective (stored vs contract_anomaly) compare.
# Higher rank = more severe. 'low' is the pre-2026-06 alias for Informational.
_BAND_RANK = {"critical": 4, "high": 3, "moderate": 2, "informational": 1, "low": 1}
# The stats count each band contributes to (mirrors get_class_scores_stats keys).
_BAND_COUNT_KEY = {
    "critical": "critical_count",
    "high": "high_count",
    "moderate": "moderate_count",
    "informational": "informational_count",
    "low": "informational_count",
}
# Bands the timeseries (and the Critical+High KPI) count as an alert;
# derived from the canonical pair so this overlay can never disagree with
# the base predicate it adjusts (clickhouse_scores.get_alert_timeseries).
_ALERT_BANDS = frozenset(band.lower() for band in ALERT_BANDS)


async def _flagged_effective(
    network: str,
) -> dict[str, tuple[str, float, str, float]]:
    """For every contract_anomaly-flagged tx on a network, return
    ``{tx_hash: (stored_band, stored_score, effective_ca_band, effective_ca_score)}``
    (bands lowercase).

    The host counts/orders on the STORED 9-class score, so a tx whose sidecar
    verdict outranks its stored score is undercounted. This resolves each flagged
    tx's contract_anomaly score/band (via the same projection the merge uses)
    alongside its stored score/band so the read endpoints can reconcile to the
    effective values. Archived / unscored txs are absent
    (``get_class_scores_by_hashes`` applies the same archive anti-join the
    stats/timeseries do, so they stay excluded)."""
    flagged = await clustering_queries.flagged_for_network_async(network)
    if not flagged:
        return {}
    stored_rows = await clickhouse.get_class_scores_by_hashes_async(
        network,
        list(flagged),
    )
    stored = {
        r["tx_hash"]: (str(r["risk_band"]).lower(), float(r["max_score"])) for r in stored_rows
    }
    out: dict[str, tuple[str, float, str, float]] = {}
    for tx, rows in flagged.items():
        s = stored.get(tx)
        if s is None:  # archived / unscored: excluded from the host aggregates
            continue
        resolved = ca_projection.resolve(rows)
        if resolved is None:
            continue
        sb, ss = s
        out[tx] = (sb, ss, resolved["risk_band"].value.lower(), float(resolved["score"]))
    return out


async def _list_contract_anomaly_results(
    network: str,
    *,
    bands: list[str] | None,
    min_score: float,
    analyzed_from: datetime | None,
    analyzed_to: datetime | None,
    min_corroboration: int,
    sort: str,
    limit: int,
    offset: int,
) -> tuple[list[ClassScoreResult], int]:
    """List page for ``attack_class=contract_anomaly``.

    The synthetic class is a read-time overlay with no ``tx_class_scores``
    column, so the SQL path can't filter it (``max_class`` is never stored as
    contract_anomaly). This resolves every flagged tx's effective max_class and
    keeps the ones whose sidecar verdict projects ABOVE the stored 9-class max
    (the only case the merge sets ``max_class = contract_anomaly``), which is the
    in-memory analogue of the DB's ``max_class = attack_class`` predicate. It
    then applies the same score/band/window/corroboration filters the SQL path
    applies to the stored classes, sorts identically, and paginates.

    Bounded by the flagged-set fetch cap (``flagged_for_network_async``);
    truncation is logged, never silent. Archived false positives are excluded by
    ``get_class_scores_by_hashes_async``'s default anti-join, matching the list
    query. Returns ``(page, total)`` where total is the full match count."""
    flagged = await clustering_queries.flagged_for_network_async(network)
    if not flagged:
        return [], 0
    if len(flagged) >= clustering_queries._RESCUE_FETCH_CAP:
        # No silent caps: a truncated flagged set could omit a contract_anomaly
        # detection from this filtered view; surface it so the cap can be raised.
        logger.warning(
            "contract_anomaly list filter hit the fetch cap (%d) for %s; "
            "older flagged txs may be absent from the filtered list",
            clustering_queries._RESCUE_FETCH_CAP,
            network,
        )
    stored_rows = await clickhouse.get_class_scores_by_hashes_async(
        network,
        list(flagged),
    )
    matched: list[ClassScoreResult] = []
    for row in stored_rows:
        res = _row_to_class_score(row)
        _merge_contract_anomaly(res, flagged[res.tx_hash])
        # Only txs the verdict pushes to the top belong to this filter; one whose
        # stored 9-class score still dominates is a stored-class detection.
        if res.max_class != _CONTRACT_ANOMALY:
            continue
        if not _within_analyzed_window(res.analyzed_at, analyzed_from, analyzed_to):
            continue
        if not _passes_score_band(res.max_score, res.risk_band, min_score, bands):
            continue
        # corroboration_count is the stored 9-class signal (the synthetic class
        # never mutates it); filter on it exactly as the SQL path does.
        if min_corroboration and res.corroboration_count < min_corroboration:
            continue
        matched.append(res)
    # Mirror the SQL ORDER BY so paging is consistent with the stored-class views.
    _sort_results(matched, by_date=sort == "date")
    return matched[offset : offset + limit], len(matched)


async def _augment_stats_with_contract_anomaly(
    network: str,
    stats: dict[str, Any],
) -> None:
    """Reconcile the KPI aggregate to the EFFECTIVE per-tx score for flagged txs,
    so contract-anomaly-only detections aren't undercounted. Moves a tx from its
    stored band count to its (higher) effective band count, and raises
    ``avg_max_score`` by the per-tx score delta. Mutates ``stats`` (a fresh
    per-call copy from the cached aggregate)."""
    flagged = await _flagged_effective(network)
    delta_sum = 0.0
    for sb, ss, cb, cs in flagged.values():
        if _BAND_RANK.get(cb, 0) > _BAND_RANK.get(sb, 0):
            sk, ck = _BAND_COUNT_KEY.get(sb), _BAND_COUNT_KEY.get(cb)
            if sk and ck:
                stats[sk] = max(0, int(stats.get(sk, 0)) - 1)
                stats[ck] = int(stats.get(ck, 0)) + 1
        if cs > ss:
            delta_sum += cs - ss
    # Avg Risk: the effective score raises each flagged tx's max, so lift the mean
    # by the summed delta over the (unchanged) population size.
    total = int(stats.get("total") or 0)
    avg = stats.get("avg_max_score")
    if delta_sum and total > 0 and avg is not None:
        stats["avg_max_score"] = (float(avg) * total + delta_sum) / total


async def _augment_timeseries_with_contract_anomaly(
    network: str,
    days: int,
    data: list[dict[str, Any]],
) -> None:
    """Add flagged txs that are an alert (High/Critical) by their EFFECTIVE band
    but NOT by their stored band into the daily alert counts, bucketed on block
    date (matching the timeseries). Txs already alert-banded by their stored
    score are counted by the base query, so they are skipped to avoid double
    counting. Mutates ``data`` ([{date, count}], zero-filled). Best-effort."""
    flagged = await _flagged_effective(network)
    candidates = [
        tx
        for tx, (sb, _ss, cb, _cs) in flagged.items()
        if cb in _ALERT_BANDS and sb not in _ALERT_BANDS
    ]
    if not candidates:
        return
    dates = await clickhouse.get_tx_block_dates_async(network, candidates, days)
    by_date: dict[str, int] = {}
    for d in dates.values():
        by_date[d] = by_date.get(d, 0) + 1
    index = {row["date"]: row for row in data}
    for d, c in by_date.items():
        if d in index:  # block dates are within the window the query bounds
            index[d]["count"] += c


def _row_to_class_score(row: dict[str, Any]) -> ClassScoreResult:
    scores = {name: float(row.get(name, -1)) for name in _CLASS_NAMES}

    def _decode_json_field(key: str) -> dict[str, Any]:
        value = row.get(key, {})
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return {}
        return value or {}

    sub_scores = _decode_json_field("sub_scores")
    evidence = _decode_json_field("evidence")
    return ClassScoreResult(
        tx_hash=row["tx_hash"],
        network=row["network"],
        scores=scores,
        max_score=float(row["max_score"]),
        max_class=row["max_class"],
        risk_band=RiskBand(row["risk_band"]),
        sub_scores=sub_scores,
        evidence=evidence,
        analysis_version=row["analysis_version"],
        analyzed_at=row["analyzed_at"],
        corroboration_count=int(row.get("corroboration_count", 0) or 0),
        corroborating_classes=row.get("corroborating_classes", "") or "",
        fee=row.get("fee"),
        output_count=row.get("output_count"),
    )


async def _merge_overlay_onto_page(
    network: str,
    data: list[ClassScoreResult],
) -> None:
    """Batch-merge the clustering sidecar's verdicts into a hydrated results page.

    Mirrors the fee/output_count batch-fetch pattern: one query for the whole
    page, then an additive per-row merge (which only ever RAISES score/band, so
    it is recall-safe; it enriches each row's payload). Best-effort: a sidecar
    hiccup leaves the page un-enriched rather than failing the list. A no-op when
    clustering is disabled or the page is empty. Mutates ``data`` in place.
    """
    if not (settings.CLUSTERING_ENABLED and data):
        return
    try:
        ca_by_hash = await clustering_queries.get_contract_anomaly_batch_async(
            network,
            [d.tx_hash for d in data],
        )
        for d in data:
            ca = ca_by_hash.get(d.tx_hash)
            if ca:
                _merge_contract_anomaly(d, ca)
    except Exception as e:
        logger.warning(f"contract_anomaly batch merge failed: {e}")


async def _rescue_flagged_onto_page(
    network: str,
    data: list[ClassScoreResult],
    *,
    min_score: float,
    bands: list[str] | None,
    attack_class: str | None,
    min_corroboration: int,
    analyzed_from: datetime | None,
    analyzed_to: datetime | None,
    sort: str,
    limit: int,
    offset: int,
) -> int:
    """Recall rescue (recall-first, see CLAUDE.md): re-admit flagged txs the DB
    filter dropped, returning the count added so the caller folds it into ``total``.

    A score/band filter is applied by the DB on the STORED 9-class score, before
    the contract_anomaly merge, so a tx whose stored score misses the filter but
    whose sidecar verdict projects ABOVE it would be dropped from a filtered page.
    This re-admits those on page 1 so a filtered triage view can never hide a
    sidecar detection.

    Scope: page 1 only; score/band filters only (attack_class and
    min_corroboration are 9-class-specific, so the rescue is inactive under them).
    Rescued rows are ADDITIVE (the DB excluded them), so they never strand a DB
    row; after merging, ``data`` is re-ranked and capped back to ``limit``. For
    date sort, a rescued row must be recent enough for the page (>= the oldest
    shown, once the page is full) so an old alert isn't pulled onto a
    recent-sorted page 1.

    Not handled (inherent to a read-time overlay vs materialisation): an
    UNFILTERED score-sorted list orders on the stored score, so a
    low-stored/high-anomaly tx stays on its later page rather than jumping to
    page 1. The default view is date-sorted (where recent CA txs appear), and the
    band counts / timeseries are reconciled separately, so this is a ranking
    nuance, not a dropped detection. Mutates ``data`` in place.
    """
    date_sort = sort == "date"
    rescue_active = (
        settings.CLUSTERING_ENABLED
        and offset == 0
        and (min_score > 0 or bool(bands))
        and not attack_class
        and min_corroboration == 0
    )
    if not rescue_active:
        return 0
    rescued_total = 0
    page_full = len(data) >= limit
    try:
        flagged = await clustering_queries.flagged_for_network_async(network)
        if len(flagged) >= clustering_queries._RESCUE_FETCH_CAP:
            # No silent caps: a truncated rescue set could omit a flagged tx
            # from page 1; surface it so the cap can be raised.
            logger.warning(
                "contract_anomaly rescue hit the fetch cap (%d) for %s; "
                "older flagged txs may be absent from the first page",
                clustering_queries._RESCUE_FETCH_CAP,
                network,
            )
        present = {d.tx_hash for d in data}
        rescue_hashes = [h for h in flagged if h not in present]
        # Date sort: a rescued row older than the page's oldest shown row
        # (once full) doesn't belong on it.
        date_floor = (
            min((d.analyzed_at for d in data), default=None) if page_full and date_sort else None
        )
        if rescue_hashes:
            rescue_rows = await clickhouse.get_class_scores_by_hashes_async(
                network,
                rescue_hashes,
            )
            for r in rescue_rows:
                res = _row_to_class_score(r)
                if not _within_analyzed_window(
                    res.analyzed_at,
                    analyzed_from,
                    analyzed_to,
                ):
                    continue
                if date_floor is not None and res.analyzed_at < date_floor:
                    continue
                stored_meets = _passes_score_band(
                    res.max_score,
                    res.risk_band,
                    min_score,
                    bands,
                )
                _merge_contract_anomaly(res, flagged[res.tx_hash])
                # Genuinely rescued only: stored score missed the filter but
                # the merged score now meets it. A row whose stored score
                # already met the filter is in the normal paginated set, so
                # it must not be added to total here.
                if not stored_meets and _passes_score_band(
                    res.max_score,
                    res.risk_band,
                    min_score,
                    bands,
                ):
                    data.append(res)
                    rescued_total += 1
    except Exception:
        # Recall rescue is best-effort, so a failure just skips the rescue rather
        # than failing the page. But log at ERROR WITH the traceback: the
        # clustering reads swallow a sidecar hiccup upstream, so reaching here is
        # an UNEXPECTED in-process error (e.g. the naive/aware compare bug), which
        # at WARNING would silently stop rescuing flagged detections onto page 1.
        # Don't interpolate `network` (request-derived) into the log — the
        # exc_info traceback carries the diagnostic, and logging untrusted request
        # input is what CodeQL flags (clear-text logging of sensitive data).
        logger.error(
            "contract_anomaly rescue: unexpected error, skipping rescue",
            exc_info=True,
        )
    if rescued_total:
        # Re-rank so rescued rows interleave by the active sort, then cap to
        # `limit` so the page size is honoured (matches the SQL ORDER BY).
        _sort_results(data, by_date=date_sort)
        del data[limit:]
    return rescued_total
