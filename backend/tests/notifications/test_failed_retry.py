"""Failed-delivery dead letter and its retry sweep (recall-first guard).

A scorer alert whose every channel fails must not vanish: the delivery path
dead-letters it (``record_failure=True``) and the sweep in
``app.tasks.notifications`` re-attempts it against the CURRENT config until it
delivers, is superseded, is unrouted, or exhausts the attempt budget. The
contract_anomaly poller keeps its own retry (the every-tick re-read) and must
never write dead-letter rows.
"""

import pytest

from app import notifications
from app.config import settings
from app.db import postgres
from app.notifications import dispatcher, triggers
from app.notifications.channels.base import Dispatch
from app.notifications.payloads import ImmediateAlert
from app.tasks import notifications as tasks

pytestmark = pytest.mark.asyncio


def _payload(tx_hash="tx1", band="Critical"):
    return ImmediateAlert(
        timestamp="2026-09-14T00:00:00+00:00",
        attack_class="phishing",
        risk_score=91.5,
        risk_band=band,
        tx_hash=tx_hash,
        network="preprod",
        baseline_source="per_script",
        dashboard_url="http://localhost:8000/dashboard",
    )


@pytest.fixture
def spy(monkeypatch):
    """Stub the DB and dispatcher around the delivery path and the sweep."""
    calls = {
        "recorded": [],
        "deleted": [],
        "marked": [],
        "claim": [],
        "dispatch": [],
        "already_returns": False,
        "dispatch_returns": True,
        "due_rows": [],
    }

    async def fake_already(network, tx_hash, band, source="scorer"):
        return calls["already_returns"]

    async def fake_claim(network, tx_hash, band, source="scorer"):
        calls["claim"].append(tx_hash)
        return True

    async def fake_dispatch(payload, dispatches, attachments=None):
        calls["dispatch"].append(payload.tx_hash)
        return calls["dispatch_returns"]

    async def fake_record(network, tx_hash, band, payload, source="scorer", group_key=None):
        calls["recorded"].append((tx_hash, band, source, group_key, payload))

    async def fake_due(base_backoff_seconds, limit):
        return calls["due_rows"]

    async def fake_delete(network, tx_hash, source):
        calls["deleted"].append(tx_hash)

    async def fake_mark(network, tx_hash, source, abandoned):
        calls["marked"].append((tx_hash, abandoned))

    monkeypatch.setattr(postgres, "already_notified", fake_already)
    monkeypatch.setattr(postgres, "claim_notification", fake_claim)
    monkeypatch.setattr(postgres, "record_failed_notification", fake_record)
    monkeypatch.setattr(postgres, "due_failed_notifications", fake_due)
    monkeypatch.setattr(postgres, "delete_failed_notification", fake_delete)
    monkeypatch.setattr(postgres, "mark_failed_attempt", fake_mark)
    monkeypatch.setattr(dispatcher, "dispatch", fake_dispatch)
    return calls


def _row(tx_hash="tx1", band="Critical", attempts=1, group_key=None):
    return {
        "network": "preprod",
        "tx_hash": tx_hash,
        "source": "scorer",
        "band": band,
        "group_key": group_key,
        "payload": _payload(tx_hash, band).model_dump(),
        "attempts": attempts,
    }


async def test_total_failure_is_dead_lettered_when_asked(spy):
    spy["dispatch_returns"] = False
    status = await notifications._deliver_with_dedup(
        "preprod",
        "tx_fail",
        "Critical",
        _payload("tx_fail"),
        [Dispatch(channel="webhook", webhook_url="http://x/")],
        group="g1",
        record_failure=True,
    )
    assert status == notifications.DELIVER_FAILED
    assert len(spy["recorded"]) == 1
    tx, band, source, group_key, payload = spy["recorded"][0]
    assert (tx, band, source, group_key) == ("tx_fail", "Critical", "scorer", "g1")
    assert payload["tx_hash"] == "tx_fail"  # the sweep re-sends this exact payload


async def test_default_callers_do_not_dead_letter(spy):
    # The poller (and the sweep itself) rely on their own retry; a failure from
    # them must not write a row, or the two mechanisms would double-deliver.
    spy["dispatch_returns"] = False
    await notifications._deliver_with_dedup(
        "preprod",
        "tx_ca",
        "Critical",
        _payload("tx_ca"),
        [Dispatch(channel="webhook", webhook_url="http://x/")],
        source="contract_anomaly",
    )
    assert spy["recorded"] == []


async def test_success_and_duplicate_record_nothing(spy):
    spy["dispatch_returns"] = True
    await notifications._deliver_with_dedup(
        "preprod",
        "tx_ok",
        "Critical",
        _payload("tx_ok"),
        [Dispatch(channel="webhook", webhook_url="http://x/")],
        record_failure=True,
    )
    spy["already_returns"] = True
    await notifications._deliver_with_dedup(
        "preprod",
        "tx_dup",
        "Critical",
        _payload("tx_dup"),
        [Dispatch(channel="webhook", webhook_url="http://x/")],
        record_failure=True,
    )
    assert spy["recorded"] == []


async def test_retry_delivers_and_deletes_the_row(spy, monkeypatch):
    spy["due_rows"] = [_row("tx1")]
    spy["dispatch_returns"] = True
    monkeypatch.setattr(
        triggers,
        "resolve_dispatch",
        lambda band, cls: [Dispatch(channel="webhook", webhook_url="http://x/")],
    )
    await tasks._retry_tick()
    assert spy["dispatch"] == ["tx1"]
    assert spy["claim"] == ["tx1"]  # a delivered retry claims like any delivery
    assert spy["deleted"] == ["tx1"]
    assert spy["marked"] == []


async def test_retry_withdraws_a_row_no_longer_routed(spy, monkeypatch):
    # Current config is the single source of truth: the operator silencing the
    # (band, class) withdraws the pending retry rather than delivering against
    # a stale route.
    spy["due_rows"] = [_row("tx1")]
    monkeypatch.setattr(triggers, "resolve_dispatch", lambda band, cls: [])
    await tasks._retry_tick()
    assert spy["dispatch"] == []
    assert spy["deleted"] == ["tx1"]


async def test_retry_failure_marks_attempt_without_abandoning(spy, monkeypatch):
    spy["due_rows"] = [_row("tx1", attempts=1)]
    spy["dispatch_returns"] = False
    monkeypatch.setattr(
        triggers,
        "resolve_dispatch",
        lambda band, cls: [Dispatch(channel="webhook", webhook_url="http://x/")],
    )
    await tasks._retry_tick()
    assert spy["marked"] == [("tx1", False)]
    assert spy["deleted"] == []
    assert spy["recorded"] == []  # the sweep owns the counter; no re-insert


async def test_retry_abandons_at_the_attempt_budget(spy, monkeypatch):
    spy["due_rows"] = [_row("tx1", attempts=settings.NOTIFY_RETRY_MAX_ATTEMPTS - 1)]
    spy["dispatch_returns"] = False
    monkeypatch.setattr(
        triggers,
        "resolve_dispatch",
        lambda band, cls: [Dispatch(channel="webhook", webhook_url="http://x/")],
    )
    await tasks._retry_tick()
    assert spy["marked"] == [("tx1", True)]


async def test_retry_superseded_by_a_claim_deletes_without_send(spy, monkeypatch):
    # A rollback-driven re-score delivered meanwhile: the dedup pre-check
    # reports a claim, so the sweep drops the row instead of duplicating.
    spy["due_rows"] = [_row("tx1")]
    spy["already_returns"] = True
    monkeypatch.setattr(
        triggers,
        "resolve_dispatch",
        lambda band, cls: [Dispatch(channel="webhook", webhook_url="http://x/")],
    )
    await tasks._retry_tick()
    assert spy["dispatch"] == []
    assert spy["deleted"] == ["tx1"]


async def test_one_bad_row_does_not_stop_the_sweep(spy, monkeypatch):
    spy["due_rows"] = [
        {**_row("tx_bad"), "payload": {"not": "an alert"}},  # fails validation
        _row("tx_good"),
    ]
    spy["dispatch_returns"] = True
    monkeypatch.setattr(
        triggers,
        "resolve_dispatch",
        lambda band, cls: [Dispatch(channel="webhook", webhook_url="http://x/")],
    )
    await tasks._retry_tick()
    assert spy["dispatch"] == ["tx_good"]
    assert spy["deleted"] == ["tx_good"]
