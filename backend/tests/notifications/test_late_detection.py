"""An alert the backlog lane raises says it is late, and when its transaction was.

Recall first: a transaction that left the live window unscored is still scored
and still alerts. The payload only lets the receiver tell a fresh finding from
an old one.
"""

from datetime import UTC, datetime

from app.notifications.channels.email import _render_immediate
from app.notifications.payloads import build_contract_anomaly_alert, build_immediate_alert

TX_TIME = datetime(2026, 3, 3, 14, 20, 0, tzinfo=UTC)
SCORED_AT = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
_SCORE = 71.0


def _result(**extra):
    return {
        "tx_hash": "ab" * 32,
        "max_class": "circular",
        "max_score": _SCORE,
        "risk_band": "High",
        "sub_scores": {},
        "baseline_source": "per_script",
        "analyzed_at": SCORED_AT,
        "tx_timestamp": TX_TIME,
        **extra,
    }


def test_a_backlog_alert_is_marked_late_with_its_tx_time():
    payload = build_immediate_alert(_result(late_detection=True), "mainnet").model_dump(mode="json")
    assert payload["late_detection"] is True
    assert payload["tx_timestamp"] == "2026-03-03T14:20:00Z"
    # The detection time is still the score's.
    assert payload["timestamp"] == "2026-11-02T12:00:00Z"


def test_a_live_alert_is_not_late():
    assert build_immediate_alert(_result(late_detection=False), "mainnet").late_detection is False


def test_a_contract_anomaly_alert_keeps_the_defaults():
    winner = {"risk_band": "High", "score": _SCORE, "scored_at": SCORED_AT}
    alert = build_contract_anomaly_alert("cd" * 32, "mainnet", winner)
    assert alert.late_detection is False
    assert alert.tx_timestamp == ""


def test_the_email_flags_a_late_detection():
    subject, body = _render_immediate(
        build_immediate_alert(_result(late_detection=True), "mainnet")
    )
    assert subject == "[TMS High, late detection] circular: 71/100"
    assert "Raised late" in body
    assert "Tx time     : 2026-03-03T14:20:00Z" in body


def test_an_on_time_email_keeps_its_subject():
    subject, body = _render_immediate(build_immediate_alert(_result(), "mainnet"))
    assert subject == "[TMS High] circular: 71/100"
    assert "Raised late" not in body
