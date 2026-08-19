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
from app.analysis.contract_identity import contract_address_of
from app.analysis.engine import _CLASS_NAMES
from app.analysis.normalise import score_to_band
from app.config import settings
from app.db import clickhouse, clustering_queries
from app.models.transaction import (
    ALERT_BANDS,
    FINDING_MIN_SCORE,
    ClassScoreResult,
    RiskBand,
)
from app.utils.datetime_utils import to_aware_utc

logger = logging.getLogger(__name__)

# The synthetic class merged in at read time from the clustering sidecar. It is
# NOT in _CLASS_NAMES (which mirrors the nine hardcoded tx_class_scores columns)
# so the per-tx write path stays untouched; it is injected after hydration.
_CONTRACT_ANOMALY = "contract_anomaly"


def _clusterable_priority(d: ClassScoreResult) -> bool:
    """False only for a row whose SURFACING detection is an un-clusterable
    contract_anomaly (DBSCAN-noise from a model that could not cluster its own
    data). Used as the LAST sort key so such rows fall to the bottom of an
    otherwise-tied group. Recall-safe: it is a final tie-break only, so it never
    reorders rows that differ on score / recency (nothing is filtered, hidden, or
    re-banded); it just keeps structural noise from sitting atop genuine findings
    of equal score. A row whose stored 9-class score dominates (max_class is not
    contract_anomaly) keeps normal priority even if the CA verdict is flagged."""
    return not (d.max_class == _CONTRACT_ANOMALY and d.contract_anomaly_unclusterable)


def _sort_results(results: list[ClassScoreResult], *, by_date: bool) -> None:
    """Re-rank a hydrated result list in place to mirror the SQL ORDER BY.

    ``by_date`` sorts (analyzed_at, max_score) descending; otherwise
    (max_score, analyzed_at) descending. Shared by the contract_anomaly list
    filter and the recall rescue so the two read paths order identically.
    ``analyzed_at`` is a required datetime on :class:`ClassScoreResult`, so the
    key never mixes None with datetime. ``_clusterable_priority`` is appended as
    the final tie-break so an un-clusterable contract_anomaly row loses only to
    an equally-scored (or equally-dated) peer, never on score/recency itself."""
    if by_date:
        results.sort(
            key=lambda d: (d.analyzed_at, d.max_score, _clusterable_priority(d)),
            reverse=True,
        )
    else:
        results.sort(
            key=lambda d: (d.max_score, d.analyzed_at, _clusterable_priority(d)),
            reverse=True,
        )


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
        # The stored contract_address was derived from the STORED winning class.
        # The sidecar verdict has just taken over as the winner, so the grouping
        # identity has to follow it to the watched target, or a contract_anomaly
        # alert would be grouped under whichever contract the displaced class
        # happened to name (or under nothing at all).
        result.contract_address = contract_address_of(_CONTRACT_ANOMALY, result.evidence)
    result.contract_anomaly_corroborates = score >= corroboration_threshold()
    result.contract_anomaly_scored_at = resolved.get("scored_at")
    result.contract_anomaly_unclusterable = bool(resolved.get("unclusterable_fit"))


def _warn_if_flagged_capped(
    flagged: dict[str, list[dict[str, Any]]],
    network: str,
    consequence: str,
) -> None:
    """Log when the flagged-set fetch was truncated by its safety cap.

    No silent caps (see CLAUDE.md): every reader of the flagged set states what a
    truncation costs *it*, so an operator can decide whether to raise the cap.
    ``consequence`` is that reader-specific half of the message.
    """
    if len(flagged) >= clustering_queries._RESCUE_FETCH_CAP:
        logger.warning(
            "contract_anomaly flagged fetch hit the cap (%d) for %s; %s",
            clustering_queries._RESCUE_FETCH_CAP,
            network,
            consequence,
        )


async def _flagged_with_stored_rows(
    network: str,
    consequence: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """The network's flagged set plus the stored 9-class row for each flagged tx.

    Both readers that reconcile the synthetic class against stored rows (the
    ``attack_class=contract_anomaly`` list and the grouped-alerts augmentation)
    need exactly this pair, hydrated the same way: archived false positives are
    excluded by ``get_class_scores_by_hashes_async``'s default anti-join, so a
    curated FP cannot re-enter through either path.
    """
    flagged = await clustering_queries.flagged_for_network_async(network)
    if not flagged:
        return {}, []
    _warn_if_flagged_capped(flagged, network, consequence)
    stored_rows = await clickhouse.get_class_scores_by_hashes_async(network, list(flagged))
    return flagged, stored_rows


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
    contract: str | None = None,
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
    flagged, stored_rows = await _flagged_with_stored_rows(
        network,
        "older flagged txs may be absent from the filtered list",
    )
    if not flagged:
        return [], 0
    matched: list[ClassScoreResult] = []
    for row in stored_rows:
        res = _row_to_class_score(row)
        _merge_contract_anomaly(res, flagged[res.tx_hash])
        # Only txs the verdict pushes to the top belong to this filter; one whose
        # stored 9-class score still dominates is a stored-class detection.
        if res.max_class != _CONTRACT_ANOMALY:
            continue
        # Post-merge, so the comparison is against the watched target the
        # verdict just installed rather than the displaced stored class's value.
        if contract is not None and res.contract_address != contract:
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
    # A tx whose stored score is below the finding floor is not in the mean's
    # population at all. If its effective score clears the floor, the anomaly
    # verdict does not merely raise an existing member, it ADDS one, so that tx
    # contributes its whole score to the numerator and one to the denominator.
    # Conflating the two cases (the previous single delta_sum) skewed the mean.
    entering_sum = 0.0
    entering_count = 0
    for sb, ss, cb, cs in flagged.values():
        if _BAND_RANK.get(cb, 0) > _BAND_RANK.get(sb, 0):
            sk, ck = _BAND_COUNT_KEY.get(sb), _BAND_COUNT_KEY.get(cb)
            if sk and ck:
                stats[sk] = max(0, int(stats.get(sk, 0)) - 1)
                stats[ck] = int(stats.get(ck, 0)) + 1
        if cs <= ss:
            continue
        if ss >= FINDING_MIN_SCORE:
            delta_sum += cs - ss
        elif cs >= FINDING_MIN_SCORE:
            entering_sum += cs
            entering_count += 1
    # Avg Risk: reconcile the mean over the FINDING population (the same floor
    # the alert list applies), not over every scored row.
    findings = int(stats.get("finding_count") or 0)
    avg = stats.get("avg_max_score")
    denominator = findings + entering_count
    if (delta_sum or entering_count) and denominator > 0:
        base = float(avg) * findings if avg is not None else 0.0
        stats["avg_max_score"] = (base + delta_sum + entering_sum) / denominator
        stats["finding_count"] = denominator


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
        contract_address=row.get("contract_address", "") or "",
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
    contract: str | None = None,
) -> int:
    """Recall rescue (recall-first, see CLAUDE.md): re-admit flagged txs the DB
    filter dropped, returning the count added so the caller folds it into ``total``.

    A score/band filter is applied by the DB on the STORED 9-class score, before
    the contract_anomaly merge, so a tx whose stored score misses the filter but
    whose sidecar verdict projects ABOVE it would be dropped from a filtered page.
    This re-admits those on page 1 so a filtered triage view can never hide a
    sidecar detection.

    Scope: page 1 only; score/band/contract filters only (attack_class and
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
        # Any predicate the DB applies to the STORED row can drop a tx the merged
        # verdict qualifies. `contract` is one of those: the column holds the
        # stored winning class's contract, so a re-homed tx is missing from a
        # contract-filtered page for a reason its score alone does not explain.
        # Without this term a group's count would exceed what its expansion lists
        # whenever no score or band filter happened to be active as well.
        and (min_score > 0 or bool(bands) or bool(contract))
        and not attack_class
        and min_corroboration == 0
    )
    if not rescue_active:
        return 0
    rescued_total = 0
    page_full = len(data) >= limit
    try:
        flagged = await clustering_queries.flagged_for_network_async(network)
        _warn_if_flagged_capped(
            flagged,
            network,
            "older flagged txs may be absent from the first page",
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
                # What the DB filter actually selected on: the STORED score/band
                # AND the STORED contract. Both halves matter, and both are read
                # before the merge rewrites them.
                stored_meets = _passes_score_band(
                    res.max_score,
                    res.risk_band,
                    min_score,
                    bands,
                ) and (contract is None or res.contract_address == contract)
                _merge_contract_anomaly(res, flagged[res.tx_hash])
                # Contract filter is checked AFTER the merge, because a winning
                # sidecar verdict rewrites contract_address to its watched
                # target: checking the stored value first would drop exactly the
                # anomaly rows this rescue exists to re-admit. Not a recall
                # trade-off, the analyst asked for one contract.
                if contract is not None and res.contract_address != contract:
                    continue
                # Genuinely rescued only: the DB filter dropped it and the merged
                # verdict now meets the filter. A row the DB already returned is
                # in the normal paginated set, so it must not be added to total
                # here. Re-homing counts as dropped: a tx whose stored score
                # passes but whose stored contract differs was excluded by the
                # contract predicate, so it is additive under this filter even
                # though its score alone would not have rescued it. Without that,
                # a group's count (reconciled to the effective target) would
                # exceed the rows its expansion can show.
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


async def _augment_groups_with_contract_anomaly(
    network: str,
    groups: list[dict[str, Any]],
    *,
    bands: list[str] | None,
    min_score: float,
    analyzed_from: datetime | None,
    analyzed_to: datetime | None,
    min_corroboration: int,
    anomaly_only: bool = False,
) -> list[str]:
    """Reconcile contract-grouped alert counts to the EFFECTIVE per-tx verdict.

    The SQL grouping keys on the STORED ``contract_address``, which was derived
    from the stored winning class. When a sidecar verdict wins it installs its
    own watched target, so a transaction can belong to a different group than
    SQL placed it in, or to a group SQL never produced at all. Without this the
    single noisiest class (contract_anomaly) would be invisible in the grouped
    view, which is precisely the view meant to tame it.

    Two corrections are applied per flagged transaction:

    1. it is added to its effective group, creating that group if SQL produced
       none for the target;
    2. that group's ``worst_score`` / ``worst_band`` / ``latest_analyzed_at``
       rise to the effective values.

    A transaction is never REMOVED from the group its stored contract names,
    even when the verdict re-homes it elsewhere. That is what keeps a group's
    count equal to what expanding it lists: the expansion is a ``?contract=``
    request, the DB filters that on the stored column, and no read path drops a
    row post-merge (see ``_merge_overlay_onto_page``). So a transaction with a
    stored finding on one contract and a winning verdict on another is counted
    under both, which is honest in both directions: each contract really does
    have a finding naming it, and each group's expansion really does list it.
    Over-counting distinct transactions is the recall-first side of that
    trade-off, and the alert the analyst opens is the same one either way.

    Mutates ``groups`` in place and returns the tx_hashes it counted under a
    contract despite an EMPTY stored contract. Those are the one case where the
    same alert would otherwise appear twice on a single page (as a group member
    and as a lone un-attributed row showing its superseded stored score), so the
    caller excludes them from the un-attributed query and its COUNT.
    Best-effort by contract: the caller treats a sidecar failure as "no
    augmentation" rather than failing the page.

    ``anomaly_only`` serves ``attack_class=contract_anomaly``: SQL cannot filter
    a class it never stores, so the caller passes an empty ``groups`` and this
    builds the whole result from transactions the verdict pushes to the top,
    which is the in-memory analogue of the DB's ``max_class = attack_class``.
    Under any OTHER attack_class filter the caller must not call this at all: the
    filter is a 9-class predicate the synthetic class cannot satisfy.
    """
    flagged, stored_rows = await _flagged_with_stored_rows(
        network,
        "some flagged txs may be missing from their contract's group",
    )
    if not flagged:
        return []
    by_address = {g["contract_address"]: g for g in groups}
    # Counted under a contract group despite an empty stored contract, so the
    # caller's un-attributed query must stop claiming them.
    rehomed_from_unattributed: list[str] = []

    def _passes(res: ClassScoreResult) -> bool:
        """The grouped view's full predicate, mirroring _score_filter_conditions."""
        if not _within_analyzed_window(res.analyzed_at, analyzed_from, analyzed_to):
            return False
        if not _passes_score_band(res.max_score, res.risk_band, min_score, bands):
            return False
        # corroboration_count is the stored 9-class signal; the synthetic class
        # never mutates it, so this reads the same value the SQL path filtered on.
        return not (min_corroboration > 0 and res.corroboration_count < min_corroboration)

    for row in stored_rows:
        res = _row_to_class_score(row)
        stored_contract = res.contract_address
        # Under anomaly_only there are no SQL groups to correct, so nothing was
        # ever counted for this tx and its stored placement is irrelevant.
        stored_counted = not anomaly_only and bool(stored_contract) and _passes(res)
        _merge_contract_anomaly(res, flagged[res.tx_hash])
        effective_contract = res.contract_address
        effective_counted = bool(effective_contract) and _passes(res)
        if anomaly_only and res.max_class != _CONTRACT_ANOMALY:
            # Its stored 9-class score still dominates, so it is a stored-class
            # detection and does not belong under this filter.
            effective_counted = False

        if not effective_counted:
            continue
        if not stored_contract:
            # Its stored row names no contract, so SQL put it in the
            # un-attributed bucket rather than in any group. It is about to be
            # counted under `effective_contract`, and it must not be in both.
            rehomed_from_unattributed.append(res.tx_hash)

        group = by_address.get(effective_contract)
        if group is None:
            group = {
                "contract_address": effective_contract,
                "alert_count": 0,
                "worst_score": 0.0,
                "worst_band": res.risk_band.value,
                "latest_analyzed_at": res.analyzed_at,
                "unclusterable_model": False,
            }
            by_address[effective_contract] = group
            groups.append(group)
        # Already counted by SQL only when it was counted under THIS contract.
        if not (stored_counted and stored_contract == effective_contract):
            group["alert_count"] = int(group["alert_count"]) + 1
        if res.max_score > float(group["worst_score"]):
            group["worst_score"] = res.max_score
            group["worst_band"] = res.risk_band.value
            # Tracks the WORST row, so it flips back off when a stored-class
            # alert outranks the un-clusterable verdict. A group marked
            # un-clusterable on a row that no longer tops it would tell the
            # operator to de-prioritise a contract on stale grounds.
            group["unclusterable_model"] = (
                res.max_class == _CONTRACT_ANOMALY and res.contract_anomaly_unclusterable
            )
        # Normalise both sides: the SQL group's latest_analyzed_at comes from
        # ClickHouse tz-NAIVE while res.analyzed_at is tz-AWARE, and comparing
        # them raw raises the naive-vs-aware TypeError this endpoint would
        # swallow into an unaugmented page.
        latest = to_aware_utc(group.get("latest_analyzed_at"))
        current = to_aware_utc(res.analyzed_at)
        if current is not None and (latest is None or current > latest):
            group["latest_analyzed_at"] = res.analyzed_at

    return rehomed_from_unattributed
