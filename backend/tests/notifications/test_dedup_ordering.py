"""Delivery-path dedup ordering (recall-first regression guard).

The immediate-alert path must record the dedup claim ONLY after a channel
actually delivers. A transient total-channel failure must leave the tx eligible
to re-notify on the next re-score, never silently drop a real alert. These tests
pin that deliver-then-claim ordering against a regression back to claim-first.
"""

import pytest

from app import notifications
from app.db import postgres
from app.notifications import dispatcher

pytestmark = pytest.mark.asyncio


@pytest.fixture
def spy(monkeypatch):
    """Stub the DB dedup calls + the dispatcher, recording invocations.

    ``already_returns`` / ``dispatch_returns`` let each test drive the two
    branch points (is this a duplicate? did delivery succeed?).
    """
    calls = {
        "already": [], "claim": [], "dispatch": [],
        "already_returns": False, "dispatch_returns": True,
    }

    async def fake_already(network, tx_hash, band, source="scorer"):
        calls["already"].append((network, tx_hash, band, source))
        return calls["already_returns"]

    async def fake_claim(network, tx_hash, band, source="scorer"):
        calls["claim"].append((network, tx_hash, band, source))
        return True

    async def fake_dispatch(payload, dispatches, attachments=None):
        calls["dispatch"].append((payload, dispatches))
        return calls["dispatch_returns"]

    monkeypatch.setattr(postgres, "already_notified", fake_already)
    monkeypatch.setattr(postgres, "claim_notification", fake_claim)
    monkeypatch.setattr(dispatcher, "dispatch", fake_dispatch)
    return calls


async def test_failed_delivery_is_not_claimed(spy):
    # Total-channel failure: dispatch delivered nothing. The tx must stay
    # unclaimed so the next re-score retries it (the recall-first fix).
    spy["dispatch_returns"] = False
    await notifications._deliver_with_dedup(
        "preprod", "tx_fail", "High", object(), [object()],
    )
    assert spy["dispatch"], "delivery should have been attempted"
    assert not spy["claim"], "a failed delivery must NOT record a dedup claim"


async def test_successful_delivery_is_claimed(spy):
    spy["dispatch_returns"] = True
    await notifications._deliver_with_dedup(
        "preprod", "tx_ok", "Critical", object(), [object()],
    )
    assert spy["dispatch"], "delivery should have been attempted"
    assert spy["claim"] == [("preprod", "tx_ok", "Critical", "scorer")], (
        "a successful delivery must record exactly one claim (default source)"
    )


async def test_duplicate_is_skipped_before_delivery(spy):
    # Already notified at >= this band: no I/O, no re-dispatch.
    spy["already_returns"] = True
    await notifications._deliver_with_dedup(
        "preprod", "tx_dup", "High", object(), [object()],
    )
    assert not spy["dispatch"], "a duplicate (>= band) must not re-dispatch"
    assert not spy["claim"]


async def test_dedup_check_failure_still_delivers(spy, monkeypatch):
    # If the dedup pre-check itself errors, prefer a possible duplicate over a
    # missed alert: delivery proceeds.
    async def boom(network, tx_hash, band, source="scorer"):
        raise RuntimeError("pg down")

    spy["dispatch_returns"] = True
    monkeypatch.setattr(postgres, "already_notified", boom)
    await notifications._deliver_with_dedup(
        "preprod", "tx_err", "Critical", object(), [object()],
    )
    assert spy["dispatch"], "a dedup-check error must not suppress the alert"


async def test_source_is_threaded_to_both_dedup_calls(spy):
    # The clustering poller delivers under a SEPARATE dedup stream so a
    # contract_anomaly alert never suppresses (or is suppressed by) the per-tx
    # scorer alert for the same transaction.
    await notifications._deliver_with_dedup(
        "preprod", "tx_ca", "High", object(), [object()],
        source="contract_anomaly",
    )
    assert spy["already"] == [("preprod", "tx_ca", "High", "contract_anomaly")]
    assert spy["claim"] == [("preprod", "tx_ca", "High", "contract_anomaly")]
