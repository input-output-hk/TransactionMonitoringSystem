"""Notification payload models.

Currently the ``immediate_alert`` schema; the ``periodic_report`` schema is a
Phase 2 add-on that rides the same dispatcher with a new model here.

The payload is the wire format delivered by every channel — the webhook posts
``model_dump(mode="json")`` verbatim, and the email renders the same fields —
so the field names and types are stable wire contracts.
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.config import settings


class ImmediateAlert(BaseModel):
    """``immediate_alert``: one high-risk transaction, dispatched now."""

    notification_type: Literal["immediate_alert"] = "immediate_alert"
    timestamp: str                       # ISO 8601 UTC (the score's analyzed_at)
    attack_class: str                    # dominant class (max_class)
    risk_score: float                    # 0-100 (max_score)
    risk_band: str                       # Informational | Moderate | High | Critical
    tx_hash: str
    network: str                         # mainnet | preprod | preview
    contributing_features: Dict[str, float] = Field(default_factory=dict)
    baseline_source: str                 # per_script | per_policy | global_fallback
    dashboard_url: str


class ReportSummary(BaseModel):
    """The summary block of a periodic report."""

    total_transactions_scored: int
    alerts_by_band: Dict[str, int]      # {Critical, High, Moderate, Informational}
    alerts_by_class: Dict[str, int]     # per attack class (+ contract_anomaly when sidecar on)
    false_positives_archived: int


class TopAlert(BaseModel):
    """One entry in a periodic report's top_alerts list."""

    tx_hash: str
    attack_class: str
    risk_score: float
    risk_band: str
    timestamp: str


class PeriodicReport(BaseModel):
    """``periodic_report``: a scheduled digest over a trailing window."""

    notification_type: Literal["periodic_report"] = "periodic_report"
    timestamp: str
    network: str
    report_window: Dict[str, str]       # {"from": iso, "to": iso}
    summary: ReportSummary
    top_alerts: List[TopAlert]
    dashboard_url: str


def _top_features(
    sub_scores: Dict[str, Dict[str, float]], attack_class: str, n: int,
) -> Dict[str, float]:
    """Top-N sub-scores of the dominant class, by value descending.

    ``sub_scores`` is keyed by class name -> {feature: normalised [0,1] value}
    (see engine._score_transaction). Non-numeric entries are skipped.
    """
    feats = (sub_scores or {}).get(attack_class) or {}
    items = [
        (k, float(v)) for k, v in feats.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    items.sort(key=lambda kv: kv[1], reverse=True)
    return {k: round(v, 4) for k, v in items[: max(0, n)]}


def build_immediate_alert(result: Dict, network: str) -> ImmediateAlert:
    """Map an engine result dict (engine._score_transaction) -> ImmediateAlert."""
    tx_hash = result["tx_hash"]
    attack_class = result.get("max_class") or ""
    analyzed_at = result.get("analyzed_at")
    timestamp = (
        analyzed_at.isoformat() if hasattr(analyzed_at, "isoformat")
        else str(analyzed_at or "")
    )
    base = settings.APP_BASE_URL.rstrip("/")
    return ImmediateAlert(
        timestamp=timestamp,
        attack_class=attack_class,
        risk_score=result.get("max_score", 0.0),
        risk_band=result.get("risk_band", ""),
        tx_hash=tx_hash,
        network=network,
        contributing_features=_top_features(
            result.get("sub_scores", {}), attack_class, settings.NOTIFY_TOP_FEATURES,
        ),
        baseline_source=_spec_baseline_source(result.get("baseline_source")),
        dashboard_url=f"{base}/attacks/{tx_hash}",
    )


def build_contract_anomaly_alert(
    tx_hash: str, network: str, winner: Dict,
) -> ImmediateAlert:
    """Map a resolved clustering contract_anomaly verdict -> ImmediateAlert.

    ``winner`` is ``analysis.contract_anomaly.resolve(...)``'s output: the
    highest-severity raw verdict row for the tx, plus the host-scale ``score``
    (0-100) and ``risk_band`` it projects to. contract_anomaly is the sidecar's
    read-time-only class (never in the per-tx scoring path), so the clustering
    poller builds the alert from the verdict directly rather than from an engine
    result dict.
    """
    band = winner.get("risk_band")
    band_str = band.value if hasattr(band, "value") else str(band or "")
    scored_at = winner.get("scored_at")
    timestamp = (
        scored_at.isoformat() if hasattr(scored_at, "isoformat")
        else str(scored_at or "")
    )
    # Surface the discriminating raw verdict signals as "contributing features".
    feats = {
        k: round(float(winner[k]), 4)
        for k in ("consensus", "iso_score", "lof_score", "votes")
        if isinstance(winner.get(k), (int, float)) and not isinstance(winner.get(k), bool)
    }
    base = settings.APP_BASE_URL.rstrip("/")
    return ImmediateAlert(
        timestamp=timestamp,
        attack_class="contract_anomaly",
        risk_score=float(winner.get("score", 0.0)),
        risk_band=band_str,
        tx_hash=tx_hash,
        network=network,
        contributing_features=feats,
        # A clustering/consensus verdict, not a per-script/per-policy baseline,
        # so it maps to the global_fallback tier of the payload enum.
        baseline_source="global_fallback",
        dashboard_url=f"{base}/attacks/{tx_hash}",
    )


def _spec_baseline_source(raw: Optional[str]) -> str:
    """Map the engine's internal baseline tier to the payload enum.

    The engine emits per_script / per_policy / global / fixed / bootstrap /
    missing. The wire schema defines only per_script | per_policy |
    global_fallback, so everything that is not a per-script/per-policy
    baseline collapses to global_fallback.
    """
    return raw if raw in ("per_script", "per_policy") else "global_fallback"
