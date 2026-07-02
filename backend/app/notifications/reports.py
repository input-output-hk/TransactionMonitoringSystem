"""Periodic-report assembly.

Builds a :class:`PeriodicReport` payload from the existing windowed stats
queries, and resolves which channels/recipients receive it from the
``periodic_report`` config. Pure reads + payload construction; the scheduling
(when a report is due) lives in :mod:`app.tasks.notifications`.
"""

import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import urlencode

from app.config import settings
from app.db import archive_queries, clickhouse_scores
from app.models.transaction import AttackClass
from app.notifications import config
from app.notifications.channels.base import Dispatch
from app.notifications.payloads import PeriodicReport, ReportSummary, TopAlert

logger = logging.getLogger(__name__)

# Low -> high; a band "at or above" min_band is a suffix of this list.
_BAND_ORDER = ["Informational", "Moderate", "High", "Critical"]
_CLASS_NAMES = [c.value for c in AttackClass]

_LOVELACE_PER_ADA = 1_000_000

# CSV columns + order — must match the web interface's manual export
# (frontend AlertCsvRow / toCsvRow) so the automated report is the same format.
_CSV_COLUMNS = [
    "tx_hash", "analyzed_at", "network", "max_class", "max_score", "risk_band",
    "fee_ada", "output_count",
    "score_token_dust", "score_large_value", "score_large_datum",
    "score_multiple_sat", "score_front_running", "score_sandwich",
    "score_circular", "score_fake_token", "score_phishing",
    "sub_scores", "analysis_version",
]
# Same as the frontend export safety cap.
_CSV_HARD_CAP = 50_000


def _bands_at_or_above(min_band: str) -> List[str]:
    if min_band not in _BAND_ORDER:
        return list(_BAND_ORDER)
    return _BAND_ORDER[_BAND_ORDER.index(min_band):]


def effective_window_days(frequency: str, window_days: int) -> int:
    """Daily reports cover the preceding 24h regardless of window_days."""
    return 1 if frequency == "daily" else max(1, window_days)


def report_interval(frequency: str) -> timedelta:
    return {
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
        "monthly": timedelta(days=30),
    }.get(frequency, timedelta(days=7))


def _iso(dt: Any) -> str:
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt or "")


def _date_param(dt: Any) -> str:
    """yyyy-mm-dd for the reports page date filters (<input type=date>)."""
    return dt.date().isoformat() if hasattr(dt, "date") else str(dt)[:10]


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC (ClickHouse returns naive UTC for these
    tables); pass an aware datetime through unchanged."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _in_window(stamps: List[Any], window_start: datetime, window_end: datetime) -> bool:
    """True if ANY of the given timestamps (naive treated as UTC) lands in the
    window; non-datetimes are ignored."""
    return any(
        isinstance(s, datetime) and window_start <= _as_utc(s) <= window_end
        for s in stamps
    )


async def _contract_anomaly_in_window(
    network: str, window_start: datetime, window_end: datetime, min_band: str,
) -> List[Dict[str, Any]]:
    """Resolved sidecar contract_anomaly findings at/above ``min_band`` that fall
    in the window, highest score first. Powers BOTH the per-class count and the
    top-alerts fold, so the summary number and the listed rows come from one set.

    A finding is in-window if EITHER the winner's ``scored_at`` OR any of the
    tx's rows' ``published_at`` lands in it. ``scored_at`` is the ORIGINAL
    scoring time and stays fixed across relabels; ``published_at`` is bumped on
    every reconciliation. Windowing on ``scored_at`` alone would drop a tx
    relabeled malicious THIS window (fresh published_at, old scored_at) from
    every report, i.e. exactly the human-confirmed findings that matter most,
    even though the poller alerted on it. Reuses :func:`contract_anomaly.resolve`
    so the report agrees with the alerts that fired.

    Best-effort: on a sidecar miss/outage the window counts 0 rather than failing
    the whole report, but the outage is logged (a silent 0 in a client-facing
    compliance report would misrepresent "no findings"). Cap-truncation is logged
    inside :func:`clustering_queries.flagged_for_network` (on RAW rows, so a
    multi-target tx can't hide a truncation the way a grouped count would).
    """
    from app.analysis import contract_anomaly as ca
    from app.db import clustering_queries

    try:
        flagged = await clustering_queries.flagged_for_network_async(
            network, raise_on_error=True,
        )
    except Exception:
        logger.warning(
            "periodic report: contract_anomaly fetch failed for %s; counting 0 "
            "for this window (sidecar unreachable)", network, exc_info=True,
        )
        return []
    allowed = set(_bands_at_or_above(min_band))
    found: List[Dict[str, Any]] = []
    for rows in flagged.values():
        winner = ca.resolve(rows)
        if winner is None:
            continue
        raw_band = winner.get("risk_band")
        band = raw_band.value if hasattr(raw_band, "value") else str(raw_band or "")
        if band not in allowed:
            continue
        stamps = [winner.get("scored_at"), *(r.get("published_at") for r in rows)]
        if _in_window(stamps, window_start, window_end):
            found.append(winner)
    found.sort(key=lambda w: float(w.get("score") or 0.0), reverse=True)
    return found


async def build_periodic_report(
    network: str, window_start: datetime, window_end: datetime, cfg: Dict[str, Any],
) -> PeriodicReport:
    """Assemble the periodic_report payload for the given window."""
    min_band = cfg.get("min_band", "Moderate")
    attack_classes = cfg.get("attack_classes", "all")
    top_n = settings.NOTIFY_REPORT_TOP_ALERTS

    bands = _bands_at_or_above(min_band)
    in_scope = (
        set(_CLASS_NAMES) if attack_classes == "all"
        else {c for c in attack_classes if c in _CLASS_NAMES}
    )

    # One GROUP BY over the window for band + class counts.
    # by_band spans all bands (total = their sum); by_class counts only
    # rows at/above min_band, which we then narrow to the in-scope classes.
    counts = await clickhouse_scores.aggregate_window_counts_async(
        network, window_start, window_end, bands,
    )
    total = counts["total"]
    alerts_by_band = {
        band: counts["by_band"].get(band, 0)
        for band in ("Critical", "High", "Moderate", "Informational")
    }
    alerts_by_class = {
        cls: (counts["by_class"].get(cls, 0) if cls in in_scope else 0)
        for cls in _CLASS_NAMES
    }
    # contract_anomaly is the sidecar's read-time-only class: it never lands in
    # tx_class_scores, so the GROUP BY above always counts it 0. When the sidecar
    # is enabled (and the class is in scope), replace that 0 with the real count
    # of flagged verdicts in the window so the per-class breakdown matches the
    # immediate alerts the poller fires. The same findings are folded into
    # top_alerts below (they can never come from the tx_class_scores query).
    # Intentionally NOT added to total_transactions_scored or alerts_by_band:
    # those are per-transaction scorer stats and a flagged tx is already counted
    # there by its 9-class score; folding it in again would double-count the tx.
    ca_findings: List[Dict[str, Any]] = []
    if settings.CLUSTERING_ENABLED and "contract_anomaly" in in_scope:
        ca_findings = await _contract_anomaly_in_window(
            network, window_start, window_end, min_band,
        )
        alerts_by_class["contract_anomaly"] = len(ca_findings)

    false_positives = await archive_queries.archive_count_async(
        network, date_from=window_start, date_to=window_end,
    )

    # Top alerts by score (>= min_band) in the window. When the report is
    # scoped to a class subset we over-fetch and post-filter to the subset.
    fetch_limit = top_n if attack_classes == "all" else max(top_n * 5, 50)
    rows = await clickhouse_scores.get_class_scores_list_async(
        network, bands, None, 0.0,
        sort="score", limit=fetch_limit, offset=0,
        analyzed_from=window_start, analyzed_to=window_end,
    )
    top_alerts: List[TopAlert] = []
    for r in rows:
        if r.get("max_class") not in in_scope:
            continue
        top_alerts.append(TopAlert(
            tx_hash=r["tx_hash"],
            attack_class=r.get("max_class") or "",
            risk_score=r.get("max_score", 0.0),
            risk_band=r.get("risk_band", ""),
            timestamp=_iso(r.get("analyzed_at")),
        ))
        if len(top_alerts) >= top_n:
            break

    # Fold the contract_anomaly findings into the top list: they can never come
    # from the tx_class_scores query above, so without this the summary could
    # report contract_anomaly: N while the top list shows none of them. Merge,
    # re-rank by score, and truncate so the list is the true top-N across both
    # sources. (The attached CSV still mirrors the web UI's manual export, which
    # is 9-class-only; see build_report_csv.)
    for w in ca_findings:
        raw_band = w.get("risk_band")
        top_alerts.append(TopAlert(
            tx_hash=w.get("tx_hash", ""),
            attack_class="contract_anomaly",
            risk_score=float(w.get("score") or 0.0),
            risk_band=raw_band.value if hasattr(raw_band, "value") else str(raw_band or ""),
            timestamp=_iso(w.get("scored_at")),
        ))
    if ca_findings:
        top_alerts.sort(key=lambda a: a.risk_score, reverse=True)
        top_alerts = top_alerts[:top_n]

    base = settings.APP_BASE_URL.rstrip("/")
    return PeriodicReport(
        timestamp=_iso(window_end),
        network=network,
        report_window={"from": _iso(window_start), "to": _iso(window_end)},
        summary=ReportSummary(
            total_transactions_scored=total,
            alerts_by_band=alerts_by_band,
            alerts_by_class=alerts_by_class,
            false_positives_archived=false_positives,
        ),
        top_alerts=top_alerts,
        # Link to the aggregated report view (full sub-score detail for these
        # txs), NOT a single transaction. Pre-scope it to the report
        # window and sort by score so the page opens with these top alerts
        # leading (the reports page seeds its filters from these query params).
        dashboard_url=f"{base}/reports?" + urlencode({
            "from": _date_param(window_start),
            "to": _date_param(window_end),
            "sort": "score",
        }),
    )


def _alert_csv_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """One CSV row in the manual-export shape (frontend toCsvRow)."""
    fee = r.get("fee")
    output_count = r.get("output_count")
    max_class = r.get("max_class") or ""
    sub = r.get("sub_scores") or {}
    return {
        "tx_hash": r.get("tx_hash", ""),
        "analyzed_at": _iso(r.get("analyzed_at")),
        "network": r.get("network", ""),
        "max_class": max_class,
        "max_score": r.get("max_score", 0.0),
        "risk_band": r.get("risk_band", ""),
        "fee_ada": (fee / _LOVELACE_PER_ADA) if fee is not None else "",
        "output_count": output_count if output_count is not None else "",
        "score_token_dust": r.get("token_dust", -1),
        "score_large_value": r.get("large_value", -1),
        "score_large_datum": r.get("large_datum", -1),
        "score_multiple_sat": r.get("multiple_sat", -1),
        "score_front_running": r.get("front_running", -1),
        "score_sandwich": r.get("sandwich", -1),
        "score_circular": r.get("circular", -1),
        "score_fake_token": r.get("fake_token", -1),
        "score_phishing": r.get("phishing", -1),
        "sub_scores": json.dumps(sub.get(max_class, {})),
        "analysis_version": r.get("analysis_version", ""),
    }


async def build_report_csv(
    network: str, window_start: datetime, window_end: datetime, cfg: Dict[str, Any],
) -> bytes:
    """Assemble the full per-transaction CSV for the report window.

    Same format the web interface produces for manual download (one row per
    alert at/above min_band, in scope, ordered by score). Paginated to the
    same hard cap as the frontend export.

    Scope note: this covers the nine STORED scorer classes only (the
    tx_class_scores columns), deliberately matching the web UI's manual export
    byte-for-byte. contract_anomaly is read-time-only and has no per-tx row here,
    so it is summarised/counted in the report body (see build_periodic_report)
    but does not appear in this attachment.
    """
    min_band = cfg.get("min_band", "Moderate")
    attack_classes = cfg.get("attack_classes", "all")
    bands = _bands_at_or_above(min_band)
    in_scope = (
        set(_CLASS_NAMES) if attack_classes == "all"
        else {c for c in attack_classes if c in _CLASS_NAMES}
    )

    rows: List[Dict[str, Any]] = []
    offset = 0
    page_size = 1000
    while len(rows) < _CSV_HARD_CAP:
        page = await clickhouse_scores.get_class_scores_list_async(
            network, bands, None, 0.0,
            sort="score", limit=page_size, offset=offset,
            analyzed_from=window_start, analyzed_to=window_end,
        )
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    if attack_classes != "all":
        rows = [r for r in rows if r.get("max_class") in in_scope]
    rows = rows[:_CSV_HARD_CAP]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(_alert_csv_row(r))
    return buf.getvalue().encode("utf-8")


def report_dispatches(cfg: Dict[str, Any]) -> List[Dispatch]:
    """Resolve the report's channels -> Dispatch list from config.

    Recipients default to the channel's global list when the report block
    leaves them empty (the "Global" default).
    """
    out: List[Dispatch] = []
    report_recipients = cfg.get("recipients") or []
    for ch in cfg.get("channels", []):
        if not config.channel_enabled(ch):
            logger.warning("periodic report: channel '%s' is disabled; skipping", ch)
            continue
        if ch == "webhook":
            recipients: List[str] = []
            webhook_url = config.webhook_default_url() or None
        else:
            # report recipients override the channel default; empty => "Global"
            recipients = (
                config.resolve_recipients(report_recipients) if report_recipients
                else config.channel_recipients(ch)
            )
            webhook_url = None
        if not recipients and not webhook_url:
            logger.warning(
                "periodic report: channel '%s' has no recipients/URL; skipping", ch,
            )
            continue
        out.append(Dispatch(channel=ch, recipients=recipients, webhook_url=webhook_url))
    return out
