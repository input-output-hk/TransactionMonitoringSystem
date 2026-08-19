"""API endpoints for the multi-class Analysis Engine"""

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Security
from pydantic import BaseModel, Field

from app.analysis.contract_identity import NO_CONTRACT
from app.analysis.engine import _CLASS_NAMES
from app.api._params import NetworkParam, PageLimit, PageOffset, TimeFromParam, TimeToParam
from app.api.contract_anomaly_read import (
    _CONTRACT_ANOMALY,
    _augment_groups_with_contract_anomaly,
    _augment_stats_with_contract_anomaly,
    _augment_timeseries_with_contract_anomaly,
    _list_contract_anomaly_results,
    _merge_contract_anomaly,
    _merge_overlay_onto_page,
    _rescue_flagged_onto_page,
    _row_to_class_score,
)
from app.auth import verify_api_key
from app.config import settings
from app.db import archive_queries, clickhouse, clustering_queries
from app.models.common import ListResponse
from app.models.transaction import ClassScoreResult, RiskBand
from app.utils.datetime_utils import UtcDateTime, format_iso_utc, to_aware_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])

# _CLASS_NAMES is imported from app.analysis.engine (the canonical scorer-order
# source) so the API's class validation can't drift from the engine's order.

# Attack classes the list filter accepts. The nine stored classes are filterable
# by the SQL path (max_class = attack_class); contract_anomaly is a read-time
# overlay with no DB column, so it is filtered in Python (see
# _list_contract_anomaly_results). It stays out of _CLASS_NAMES so the engine's
# scorer-order contract is unaffected.
_VALID_ATTACK_CLASSES = (*_CLASS_NAMES, _CONTRACT_ANOMALY)


class GroupedAlertRow(BaseModel):
    """One row of the grouped risk-alerts table.

    Two shapes share one ordered, paginated list, discriminated by ``kind``:

    - ``group``: a contract's alerts collapsed behind an expandable row.
    - ``alert``: a single alert that names no contract. Those are returned here
      rather than in a separate request so ordering and pagination stay correct.
      Paginating groups and un-attributed alerts as two independent lists would
      interleave them wrongly across page boundaries and double-count the pager.
    """

    kind: Literal["group", "alert"] = Field(
        ...,
        description="'group' = a contract's collapsed alerts, 'alert' = one un-attributed alert",
    )
    contract_address: str = Field(
        "",
        description="The contract implicated. Always empty for kind='alert'.",
    )
    alert_count: int = Field(
        ...,
        description=(
            "Alerts under this row, using the SAME filters the flat list applies. "
            "A true server-side count, not a page-window artefact, so it is safe "
            "to display as the total. Always 1 for kind='alert'."
        ),
    )
    worst_score: float = Field(..., description="Highest max_score under this row (0-100)")
    worst_band: RiskBand = Field(
        ...,
        description=(
            "For a group, the STORED band of its highest-scoring alert rather "
            "than a band re-derived from worst_score, so the badge agrees with "
            "the row the analyst sees on expanding it."
        ),
    )
    latest_analyzed_at: UtcDateTime = Field(
        ...,
        description="Most recent analyzed_at under this row",
    )
    tx_hash: str | None = Field(
        None,
        description="The transaction, for kind='alert' only",
    )
    attack_class: str | None = Field(
        None,
        description=(
            "The winning class of the alert this row stands for: the alert "
            "itself for kind='alert', and the group's highest-scoring alert for "
            "kind='group'. Same row worst_band and worst_score describe, so a "
            "group names an attack type instead of only counting alerts."
        ),
    )
    attack_classes: list[str] = Field(
        default_factory=list,
        description=(
            "Every distinct class under this row, so a client can say how many "
            "OTHER kinds of alert a group holds without expanding it. One "
            "element for kind='alert'. Bounded by the nine-class vocabulary. A "
            "contract_anomaly verdict is unioned in, never subtracted: see "
            "_augment_groups_with_contract_anomaly."
        ),
    )
    unclusterable_model: bool = Field(
        False,
        description=(
            "Mirrors the flat row's un-clusterable contract_anomaly marker: True "
            "when this row's worst alert is a sidecar verdict whose model could "
            "not cluster the contract, so the operator can de-prioritise it."
        ),
    )


async def _unattributed_alert_rows(
    network: str,
    *,
    risk_band: list[str] | None,
    attack_class: str | None,
    min_score: float,
    sort: str,
    analyzed_from: Any,
    analyzed_to: Any,
    min_corroboration: int,
    skip: int,
    need: int,
    exclude_tx_hashes: list[str],
) -> tuple[list[dict[str, Any]], int]:
    """Alerts that name no contract, shaped as rows of the grouped list.

    Selected with the tri-state contract filter's empty-string value, i.e. the
    complement of what the grouped aggregate returns, so the two together cover
    every alert the flat list would show exactly once.

    These render as ordinary rows: an alert with no contract has nothing to put
    behind a chevron, and a single "Unattributed" bucket would hide four
    unrelated attack classes behind one meaningless label.

    Returns ``(rows, total)``. Only the window ``[skip, skip + need)`` is fetched,
    in the SAME order the caller merges on, because that window is provably the
    only part of this list that can surface on the requested page (see the caller
    for the bound). ``total`` is a separate COUNT so the pager still reports every
    matching alert rather than the fetched slice.

    ``exclude_tx_hashes`` drops the transactions the contract_anomaly
    reconciliation has already counted under a contract group. Their STORED
    contract is empty, so this query would otherwise claim them as well and one
    alert would render twice on one page, once as a group member and once as a
    lone row showing its now-superseded stored score. Applying it to the COUNT
    too keeps the pager exact.
    """
    rows = await clickhouse.get_class_scores_list_async(
        network=network,
        risk_band=risk_band,
        attack_class=attack_class,
        min_score=min_score,
        sort=sort,
        analyzed_from=analyzed_from,
        analyzed_to=analyzed_to,
        limit=need,
        offset=skip,
        min_corroboration=min_corroboration,
        contract=NO_CONTRACT,
        exclude_tx_hashes=exclude_tx_hashes,
    )
    total = await clickhouse.count_class_scores_async(
        network=network,
        risk_band=risk_band,
        attack_class=attack_class,
        min_score=min_score,
        analyzed_from=analyzed_from,
        analyzed_to=analyzed_to,
        min_corroboration=min_corroboration,
        contract=NO_CONTRACT,
        exclude_tx_hashes=exclude_tx_hashes,
    )
    return [
        {
            "kind": "alert",
            "contract_address": "",
            "alert_count": 1,
            "worst_score": float(r["max_score"]),
            "worst_band": r["risk_band"],
            "latest_analyzed_at": r["analyzed_at"],
            "tx_hash": r["tx_hash"],
            "attack_class": r["max_class"],
            "attack_classes": [r["max_class"]],
            "unclusterable_model": False,
        }
        for r in rows
    ], total


class GroupedAlertsResponse(ListResponse[GroupedAlertRow]):
    """The grouped list plus the alert count its pager cannot express.

    ``total`` counts ROWS, because rows are what the pager steps through: one
    contract group is one row however many alerts it stands for. ``alert_total``
    is the number of ALERTS under the same filters, which is what an operator
    reads beside a table of groups. It is the sum of exactly the ``alert_count``
    values these rows carry, so it always agrees with what the screen shows and
    with what each group's expansion lists; a transaction that implicates two
    contracts therefore counts once per contract.
    """

    alert_total: int


# NOTE: declared BEFORE /results/{tx_hash}. FastAPI matches routes in
# declaration order, so this literal segment must precede the path-parameter
# route that would otherwise capture "grouped" as a tx_hash.
@router.get(
    "/results/grouped",
    dependencies=[Security(verify_api_key)],
    response_model=GroupedAlertsResponse,
)
async def list_analysis_result_groups(
    network: NetworkParam = None,
    risk_band: list[RiskBand] = Query(
        default_factory=list,
        description="Filter by risk band. Repeat the param to OR-match multiple values.",
    ),
    attack_class: str | None = Query(None, description="Filter by attack class name"),
    min_score: float = Query(0.0, ge=0.0, le=100.0, description="Minimum score filter"),
    min_corroboration: int = Query(0, ge=0, le=len(_CLASS_NAMES)),
    sort: str = Query("date", description="Group order: 'score' or 'date'"),
    analyzed_from: TimeFromParam = None,
    analyzed_to: TimeToParam = None,
    limit: PageLimit = 100,
    offset: PageOffset = 0,
):
    """Alerts collapsed to one row per contract, for the grouped alerts table.

    Applies the same filters as ``/results`` so a group's ``alert_count`` matches
    what the flat list returns for that contract. Alerts naming no contract are
    returned in the SAME list as ungrouped ``kind='alert'`` rows: paginating them
    separately would interleave the two shapes wrongly across page boundaries and
    leave neither pager knowing the combined total.

    Pagination is applied in Python, after the contract_anomaly reconciliation,
    because a winning sidecar verdict can move a transaction between groups (or
    create one) and SQL LIMIT/OFFSET cannot account for that. Group cardinality
    is distinct contracts, orders of magnitude below alert cardinality, so the
    full set is cheap to fetch; the fetch is bounded by
    ``GROUPED_ALERTS_MAX_CONTRACTS`` and hitting that bound is logged. The
    un-attributed side arrives DB-ordered, so only the window that can reach the
    requested page is fetched (bounded by that same contract cap plus the page
    size, whatever the offset) and its total comes from a COUNT.
    """
    if attack_class and attack_class not in _VALID_ATTACK_CLASSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown attack class '{attack_class}'. Valid: {list(_VALID_ATTACK_CLASSES)}",
        )
    if sort not in ("score", "date"):
        raise HTTPException(status_code=422, detail="sort must be 'score' or 'date'")
    query_network = network or settings.CARDANO_NETWORK
    try:
        rbs = [b.value for b in risk_band] if risk_band else None
        anomaly_only = attack_class == _CONTRACT_ANOMALY
        if anomaly_only and not settings.CLUSTERING_ENABLED:
            # The class cannot exist with clustering off, so the page is
            # legitimately empty rather than an error (matching /results).
            return {"count": 0, "total": 0, "alert_total": 0, "data": []}
        cap = settings.GROUPED_ALERTS_MAX_CONTRACTS
        groups: list[dict[str, Any]] = []
        if not anomaly_only:
            # contract_anomaly is never a STORED max_class, so SQL cannot filter
            # for it and must not be asked to: querying unfiltered and letting
            # the reconciliation add to it would return every contract group.
            # Under that filter the reconciliation builds the whole result.
            groups = await clickhouse.group_class_scores_by_contract_async(
                network=query_network,
                risk_band=rbs,
                attack_class=attack_class,
                min_score=min_score,
                sort=sort,
                analyzed_from=analyzed_from,
                analyzed_to=analyzed_to,
                limit=cap,
                offset=0,
                min_corroboration=min_corroboration,
            )
            if len(groups) >= cap:
                logger.warning(
                    "grouped alerts hit the contract cap (%d); some contracts are absent "
                    "from the grouped view. Raise GROUPED_ALERTS_MAX_CONTRACTS.",
                    cap,
                )
        # Mirrors the flat list's rescue gate (_rescue_flagged_onto_page): a
        # 9-class attack_class filter is not a predicate the synthetic class can
        # satisfy, so reconciling under it would add groups the filter excludes
        # and decrement groups SQL never counted. The one exception is the filter
        # BEING contract_anomaly, where the reconciliation is the whole answer.
        reconcile = settings.CLUSTERING_ENABLED and (anomaly_only or not attack_class)
        # Transactions the reconciliation counts under a contract group even
        # though their STORED contract is empty. The un-attributed query selects
        # on that stored column, so without this it would claim them too.
        rehomed: list[str] = []
        if reconcile:
            # Best-effort: the sidecar being down must not fail the alerts table.
            # Exception under anomaly_only, where the augmentation IS the result:
            # degrading to stored groups would silently answer a different
            # question, so the caller gets a 503 instead.
            try:
                rehomed = await _augment_groups_with_contract_anomaly(
                    query_network,
                    groups,
                    bands=rbs,
                    min_score=min_score,
                    analyzed_from=analyzed_from,
                    analyzed_to=analyzed_to,
                    min_corroboration=min_corroboration,
                    anomaly_only=anomaly_only,
                )
            except Exception:
                logger.error(
                    "contract_anomaly group augmentation failed",
                    exc_info=True,
                )
                if anomaly_only:
                    raise HTTPException(
                        status_code=503,
                        detail="contract_anomaly grouping unavailable",
                    ) from None
        rows: list[dict[str, Any]] = [
            {
                "kind": "group",
                "contract_address": g["contract_address"],
                "alert_count": int(g["alert_count"]),
                "worst_score": float(g["worst_score"]),
                "worst_band": g["worst_band"],
                "latest_analyzed_at": g["latest_analyzed_at"],
                "attack_class": g.get("worst_class"),
                "attack_classes": list(g.get("classes") or []),
                "unclusterable_model": bool(g.get("unclusterable_model", False)),
            }
            for g in groups
        ]
        group_rows = len(rows)
        total = group_rows
        alert_total = sum(int(g["alert_count"]) for g in groups)
        # How many un-attributed rows the fetch below skips. Every skipped row
        # ranks above the requested page, so the page shifts left by exactly this
        # much (see the slice at the end).
        page_skip = 0
        # Alerts naming no contract join the SAME list rather than being fetched
        # separately by the client. Two independently paginated lists cannot be
        # interleaved correctly across page boundaries, and neither pager would
        # know the combined total.
        if not anomaly_only:
            # Only a window of the un-attributed list can reach the requested
            # page. A group row can only push an un-attributed row DOWN the merged
            # order, and there are exactly `group_rows` of them, so an
            # un-attributed row landing at merged position p has un-attributed
            # rank in [p - group_rows, p]. Fetching [offset - group_rows,
            # offset + limit) therefore covers the page and nothing else.
            #
            # The window matters: `offset` has no upper bound (PageOffset is
            # ge=0 only), so a fetch sized offset + limit is caller-controlled and
            # could materialise the whole un-attributed set. This one is bounded
            # by GROUPED_ALERTS_MAX_CONTRACTS + limit whatever the offset.
            page_skip = max(0, offset - group_rows)
            unattributed, unattributed_total = await _unattributed_alert_rows(
                query_network,
                risk_band=rbs,
                attack_class=attack_class,
                min_score=min_score,
                sort=sort,
                analyzed_from=analyzed_from,
                analyzed_to=analyzed_to,
                min_corroboration=min_corroboration,
                skip=page_skip,
                need=offset + limit - page_skip,
                exclude_tx_hashes=rehomed,
            )
            rows.extend(unattributed)
            total += unattributed_total
            alert_total += unattributed_total
        # One ordering over both shapes, so a group and a lone alert interleave by
        # the same key the flat list sorts on.
        sort_key = (
            (lambda r: (r["worst_score"], to_aware_utc(r["latest_analyzed_at"])))
            if sort == "score"
            else (lambda r: (to_aware_utc(r["latest_analyzed_at"]), r["worst_score"]))
        )
        rows.sort(key=sort_key, reverse=True)
        # Each row `page_skip` dropped has un-attributed rank < offset - group_rows
        # and at most `group_rows` groups can precede it, so its position in the
        # full merge is strictly below `offset`: all of them sit before the page,
        # and the page is simply shifted left by that count.
        page_start = offset - page_skip
        page = rows[page_start : page_start + limit]
        return {
            "count": len(page),
            "total": total,
            "alert_total": alert_total,
            "data": page,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error grouping results: {e}")
        raise HTTPException(status_code=500, detail="Failed to group results")


@router.get("/results/{tx_hash}", dependencies=[Security(verify_api_key)])
async def get_analysis_result(
    tx_hash: str,
    network: NetworkParam = None,
) -> ClassScoreResult:
    """Full 9-class score vector with sub-score drill-down for a single transaction.

    ``network`` defaults to the configured network; it scopes the lookup so a
    tx_hash that also exists on another network cannot return the wrong row.

    If the transaction has been admin-archived as a false positive, the score is
    still returned (for audit context) and the ``archived`` field is populated
    so the UI can render it differently.
    """
    query_network = network or settings.CARDANO_NETWORK
    try:
        row = await clickhouse.get_class_scores_async(tx_hash, query_network)
    except Exception as e:
        logger.error(f"Error fetching result for {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch result")
    if not row:
        raise HTTPException(status_code=404, detail=f"No result found for {tx_hash}")
    result = _row_to_class_score(row)
    try:
        archive_meta = await archive_queries.archive_get_async(
            row["network"],
            row["tx_hash"],
        )
        if archive_meta:
            result.archived = {
                "note": archive_meta["note"],
                "archived_by": archive_meta["archived_by"],
                "archived_at": format_iso_utc(archive_meta["archived_at"]),
                "source": archive_meta["source"],
            }
    except Exception as e:
        # Archive enrichment is best-effort; never fail the main fetch.
        logger.warning(f"Archive enrichment failed for {tx_hash}: {e}")
    if settings.CLUSTERING_ENABLED:
        try:
            ca = await clustering_queries.get_contract_anomaly_async(
                row["network"],
                row["tx_hash"],
            )
            if ca:
                _merge_contract_anomaly(result, ca)
        except Exception as e:
            # Read-time merge is best-effort; never fail the main fetch.
            logger.warning(f"contract_anomaly merge failed for {tx_hash}: {e}")
    return result


@router.get(
    "/results",
    dependencies=[Security(verify_api_key)],
    response_model=ListResponse[ClassScoreResult],
)
async def list_analysis_results(
    network: NetworkParam = None,
    risk_band: list[RiskBand] = Query(
        default_factory=list,
        description=(
            "Filter by risk band. Repeat the param to OR-match multiple "
            "values, e.g. `?risk_band=Critical&risk_band=High`."
        ),
    ),
    attack_class: str | None = Query(
        None,
        description="Filter by attack class name (e.g. phishing, sandwich)",
    ),
    min_score: float = Query(0.0, ge=0.0, le=100.0, description="Minimum score filter"),
    min_corroboration: int = Query(
        0,
        ge=0,
        le=len(_CLASS_NAMES),
        description=(
            "Only include transactions where at least this many distinct attack "
            "classes independently corroborated (scored above the corroboration "
            "threshold). 0 = no filter. Surfaces multi-signal transactions; does "
            "not change risk bands."
        ),
    ),
    sort: str = Query("score", description="Sort order: 'score' or 'date'"),
    contract: str | None = Query(
        None,
        description=(
            "Filter by the contract the alert implicates. Pass an address to "
            "restrict to that contract; pass an empty string to select only the "
            "alerts that name no contract at all (the grouped view's ungrouped "
            "rows). Omit for no filter."
        ),
    ),
    analyzed_from: TimeFromParam = None,
    analyzed_to: TimeToParam = None,
    limit: PageLimit = 100,
    offset: PageOffset = 0,
):
    """List multi-class scoring results with optional filters.

    The ``from``/``to`` window filters on ``analyzed_at`` with the shared
    half-open [from, to) convention (see ``app.api._params``).
    """
    if attack_class and attack_class not in _VALID_ATTACK_CLASSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown attack class '{attack_class}'. Valid: {list(_VALID_ATTACK_CLASSES)}",
        )
    if sort not in ("score", "date"):
        raise HTTPException(status_code=422, detail="sort must be 'score' or 'date'")
    query_network = network or settings.CARDANO_NETWORK
    try:
        # Normalize the enum list to plain strings, passing None when the
        # caller didn't supply any band so the DB layer skips the WHERE.
        rbs = [b.value for b in risk_band] if risk_band else None
        # The synthetic class has no DB column, so the SQL path can't filter it.
        # Route it to the in-memory resolver. When clustering is disabled the
        # class never exists, so the filtered page is legitimately empty (not an
        # error): the frontend offers the filter unconditionally.
        if attack_class == _CONTRACT_ANOMALY:
            if not settings.CLUSTERING_ENABLED:
                return {"count": 0, "total": 0, "data": []}
            try:
                ca_data, ca_total = await _list_contract_anomaly_results(
                    query_network,
                    bands=rbs,
                    min_score=min_score,
                    analyzed_from=analyzed_from,
                    analyzed_to=analyzed_to,
                    min_corroboration=min_corroboration,
                    sort=sort,
                    limit=limit,
                    offset=offset,
                    contract=contract,
                )
            except Exception:
                # Degrade to an empty page rather than fail the request (matching
                # the sidecar read path). But log at ERROR WITH the traceback: the
                # clustering reads already swallow a sidecar hiccup upstream
                # (returning {}), so anything reaching here is an UNEXPECTED
                # in-process error, not a routine outage. Logging it at WARNING is
                # what let a naive/aware TypeError masquerade as "no anomalies" —
                # a silent recall loss. ERROR + exc_info makes the next such bug
                # loud instead of an invisible empty page.
                logger.error(
                    "contract_anomaly list filter: unexpected error, returning empty page",
                    exc_info=True,
                )
                ca_data, ca_total = [], 0
            return {"count": len(ca_data), "total": ca_total, "data": ca_data}
        # Shared filter predicate: list and count MUST apply identical filters or
        # the pagination total drifts from the rows shown. sort/limit/offset are
        # list-only (they do not affect the count) and stay out of this dict.
        filters = dict(
            network=query_network,
            risk_band=rbs,
            attack_class=attack_class,
            min_score=min_score,
            analyzed_from=analyzed_from,
            analyzed_to=analyzed_to,
            min_corroboration=min_corroboration,
            contract=contract,
        )
        rows = await clickhouse.get_class_scores_list_async(
            **filters,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        total = await clickhouse.count_class_scores_async(**filters)
        data = [_row_to_class_score(r) for r in rows]
        # Enrich the page with sidecar verdicts, then re-admit any flagged tx the
        # DB filter dropped on its stored score (recall rescue). Both are
        # recall-safe and best-effort; see the helper docstrings.
        await _merge_overlay_onto_page(query_network, data)
        rescued_total = await _rescue_flagged_onto_page(
            query_network,
            data,
            min_score=min_score,
            bands=rbs,
            attack_class=attack_class,
            min_corroboration=min_corroboration,
            analyzed_from=analyzed_from,
            analyzed_to=analyzed_to,
            sort=sort,
            limit=limit,
            offset=offset,
            contract=contract,
        )
        return {
            "count": len(data),
            "total": total + rescued_total,
            "data": data,
        }
    except Exception as e:
        logger.error(f"Error listing results: {e}")
        raise HTTPException(status_code=500, detail="Failed to list results")


class PerClassStats(BaseModel):
    scored_count: int
    avg_score: float | None
    max_score: float | None


class AnalysisStatsOut(BaseModel):
    total: int
    critical_count: int
    high_count: int
    moderate_count: int
    informational_count: int
    avg_max_score: float | None
    finding_count: int = Field(
        0,
        description=(
            "Number of transactions behind avg_max_score: those scoring at or "
            "above the finding floor. `total` counts every scored transaction "
            "including the clean ones, so it is NOT this average's denominator."
        ),
    )
    last_analyzed_at: UtcDateTime | None
    per_class: dict[str, PerClassStats]
    pending_count: int


@router.get("/stats", dependencies=[Security(verify_api_key)], response_model=AnalysisStatsOut)
async def analysis_stats(
    network: NetworkParam = None,
):
    """Per-class score distributions, band counts, and aggregate stats."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        stats = await clickhouse.get_class_scores_stats_async(query_network)
        if settings.CLUSTERING_ENABLED:
            # Reconcile band counts to the EFFECTIVE band so contract-anomaly-only
            # detections aren't undercounted in the KPI cards. Best-effort: the
            # sidecar being down must not fail the dashboard's stats.
            try:
                await _augment_stats_with_contract_anomaly(query_network, stats)
            except Exception as e:
                logger.warning(f"contract_anomaly stats augmentation failed: {e}")
        return stats
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch stats")


@router.get("/stats/timeseries", dependencies=[Security(verify_api_key)])
async def analysis_stats_timeseries(
    network: NetworkParam = None,
    days: int = Query(14, ge=1, le=90, description="Trailing window in days"),
):
    """Daily High+Critical alert counts over a trailing window, bucketed on
    on-chain block time. Powers the dashboard sparkline. Returns a list of
    ``{date, count}`` with zero-filled gaps, oldest first."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        data = await clickhouse.get_alert_timeseries_async(query_network, days)
        if settings.CLUSTERING_ENABLED:
            # Fold contract-anomaly-only alerts (High/Critical by effective band)
            # into the daily counts so the sparkline matches the KPI cards.
            # Best-effort: never fail the timeseries on a sidecar hiccup.
            try:
                await _augment_timeseries_with_contract_anomaly(
                    query_network,
                    days,
                    data,
                )
            except Exception as e:
                logger.warning(f"contract_anomaly timeseries augmentation failed: {e}")
        return {"network": query_network, "days": days, "data": data}
    except Exception as e:
        logger.error(f"Error fetching timeseries: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch timeseries")


@router.get("/baselines/{scope_type}/{scope_id}", dependencies=[Security(verify_api_key)])
async def get_baselines(
    scope_type: str,
    scope_id: str,
    network: NetworkParam = None,
):
    """Inspect baseline percentiles for a given scope (e.g. per_script, global)."""
    query_network = network or settings.CARDANO_NETWORK
    try:
        rows = await clickhouse.get_baselines_for_scope_async(
            query_network,
            scope_type,
            scope_id,
        )
        return {
            "network": query_network,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "baselines": rows,
        }
    except Exception as e:
        logger.error(f"Error fetching baselines: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch baselines")
