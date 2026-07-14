"""contract_anomaly poller (tasks.notifications._contract_anomaly_tick).

The clustering sidecar's contract_anomaly verdict is read-time-only — it never
flows through ``on_new_scores`` — so this poller is its only notification path.
These tests pin its recall-critical behaviour: resolve each flagged verdict,
route it via the trigger matrix, and deliver through the shared deliver-then-claim
path under the dedicated ``'contract_anomaly'`` dedup source; a sidecar read
failure must SURFACE (not silently return an empty, healthy-looking tick); a
first-enablement backlog must drain across ticks under the per-tick cap without
losing a finding; a failed send must be retried on the next tick; a payload-build
error on a routed finding must still page via a degraded payload; and per-tx
failures are isolated.
"""

from datetime import datetime, timezone

import pytest

from app.analysis import contract_anomaly as ca
from app.config import settings
from app.db import clustering_queries
from app.notifications import DELIVER_DUPLICATE, DELIVER_FAILED, DELIVER_SENT
from app.notifications import triggers
from app.tasks import notifications as task

pytestmark = pytest.mark.asyncio

# A positive verdict that projects to a routable (>= floor) band via the real
# projection. Carries the raw fields build_contract_anomaly_alert surfaces.
_GOOD_ROW = {
    "verdict": "malicious",
    "consensus": 0.95,
    "iso_score": 0.8,
    "lof_score": 0.7,
    "votes": 3,
    "scored_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
    "published_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
}


@pytest.fixture
def spy(monkeypatch):
    """Stub the sidecar fetch, the trigger matrix, and the delivery helper.

    ``flagged`` drives what the sidecar returns; ``dispatches`` drives whether
    the trigger matrix routes the alert anywhere; ``status`` drives the delivery
    outcome the budget/retry logic keys on.
    """
    calls = {
        "delivered": [],
        "sent": [],
        "claimed": set(),
        "routed": [],
        "flagged": {},
        "dispatches": [object()],
        "status": DELIVER_SENT,
        "fetch_calls": 0,
    }

    async def fake_flagged(
        network, limit=clustering_queries._RESCUE_FETCH_CAP, raise_on_error=False
    ):
        calls["fetch_calls"] += 1
        return calls["flagged"]

    def fake_resolve_dispatch(band, attack_class):
        calls["routed"].append((band, attack_class))
        return calls["dispatches"]

    async def fake_deliver(network, tx_hash, band, payload, dispatches, source="scorer"):
        # "delivered" records every CALL (a visit); "sent" records only real
        # deliveries. A SENT finding is claimed, so a later re-delivery of the
        # same (source, tx_hash) becomes a DUPLICATE no-op — modelling the
        # postgres dedup ledger, without which a capped backlog could never drain.
        calls["delivered"].append(
            {
                "network": network,
                "tx_hash": tx_hash,
                "band": band,
                "payload": payload,
                "source": source,
            }
        )
        s = calls["status"]
        status = s(tx_hash) if callable(s) else s
        key = (source, tx_hash)
        if status == DELIVER_SENT and key in calls["claimed"]:
            return DELIVER_DUPLICATE
        if status == DELIVER_SENT:
            calls["claimed"].add(key)
            calls["sent"].append(tx_hash)
        return status

    monkeypatch.setattr(clustering_queries, "flagged_for_network_async", fake_flagged)
    monkeypatch.setattr(triggers, "resolve_dispatch", fake_resolve_dispatch)
    monkeypatch.setattr(task, "_deliver_with_dedup", fake_deliver)
    return calls


async def test_routed_verdict_delivers_under_contract_anomaly_source(spy):
    spy["flagged"] = {"tx_ca": [_GOOD_ROW]}
    await task._contract_anomaly_tick()

    expected_band = ca.resolve([_GOOD_ROW])["risk_band"].value
    assert len(spy["delivered"]) == 1
    d = spy["delivered"][0]
    assert d["tx_hash"] == "tx_ca"
    assert d["source"] == "contract_anomaly"  # separate dedup stream
    assert d["band"] == expected_band
    assert d["payload"].attack_class == "contract_anomaly"
    assert d["payload"].tx_hash == "tx_ca"
    assert spy["routed"] == [(expected_band, "contract_anomaly")]


async def test_unrouted_band_delivers_nothing(spy):
    # The trigger matrix routes this (band, contract_anomaly) nowhere.
    spy["flagged"] = {"tx_ca": [_GOOD_ROW]}
    spy["dispatches"] = []
    await task._contract_anomaly_tick()
    assert spy["delivered"] == []


async def test_empty_verdict_rows_are_skipped(spy):
    # resolve() returns None for an empty rows list — no delivery, no crash.
    spy["flagged"] = {"tx_empty": []}
    await task._contract_anomaly_tick()
    assert spy["delivered"] == []


async def test_no_flagged_verdicts_is_a_noop(spy):
    spy["flagged"] = {}
    await task._contract_anomaly_tick()
    assert spy["delivered"] == []


async def test_one_bad_verdict_does_not_abort_the_tick(spy):
    # A malformed verdict for one tx must be isolated so the good tx still fires.
    spy["flagged"] = {"tx_bad": [123], "tx_good": [_GOOD_ROW]}
    await task._contract_anomaly_tick()
    assert [d["tx_hash"] for d in spy["delivered"]] == ["tx_good"]


async def test_sidecar_fetch_failure_raises_not_silent(spy, monkeypatch):
    # A read failure must SURFACE (recall-first: the only CA alert path must not
    # go dark reporting a healthy empty tick).
    async def boom(network, limit=clustering_queries._RESCUE_FETCH_CAP, raise_on_error=False):
        assert raise_on_error is True  # poller must ask for the raising contract
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(clustering_queries, "flagged_for_network_async", boom)
    with pytest.raises(RuntimeError):
        await task._contract_anomaly_tick()


async def test_per_tick_cap_bounds_sends_and_drains(spy, monkeypatch):
    # More routed NEW findings than the per-tick cap: exactly `cap` deliver this
    # tick, the rest on later ticks — none lost (the anti-flood guarantee). Each
    # tick re-fetches; already-sent findings dedup for free and don't spend budget.
    monkeypatch.setattr(settings, "NOTIFY_CONTRACT_ANOMALY_MAX_ALERTS_PER_TICK", 2)
    spy["flagged"] = {f"tx{i}": [_GOOD_ROW] for i in range(5)}

    await task._contract_anomaly_tick()
    assert len(spy["sent"]) == 2
    await task._contract_anomaly_tick()
    assert len(spy["sent"]) == 4
    await task._contract_anomaly_tick()
    assert sorted(spy["sent"]) == [f"tx{i}" for i in range(5)]  # all, none lost


async def test_duplicates_do_not_consume_the_per_tick_budget(spy, monkeypatch):
    # A dedup no-op is free: with everything already-notified the poller can scan
    # the whole set in one tick and find the single new finding past the cap.
    monkeypatch.setattr(settings, "NOTIFY_CONTRACT_ANOMALY_MAX_ALERTS_PER_TICK", 1)
    order = ["dup1", "dup2", "fresh"]
    spy["flagged"] = {k: [_GOOD_ROW] for k in order}
    spy["status"] = lambda tx: DELIVER_DUPLICATE if tx.startswith("dup") else DELIVER_SENT

    await task._contract_anomaly_tick()
    # All three were visited (dups are free); exactly one real send happened.
    assert [d["tx_hash"] for d in spy["delivered"]] == order
    assert spy["sent"] == ["fresh"]


async def test_delivery_failure_is_retried_next_tick(spy):
    # A channel outage (FAILED, unclaimed) leaves the finding eligible; the next
    # tick re-fetches (no cursor) and re-attempts it, so nothing is stranded.
    spy["flagged"] = {"tx_ca": [_GOOD_ROW]}
    spy["status"] = DELIVER_FAILED
    await task._contract_anomaly_tick()
    await task._contract_anomaly_tick()
    assert len(spy["delivered"]) == 2  # re-attempted on the second tick


async def test_build_failure_still_pages_via_degraded_payload(spy, monkeypatch):
    # A payload-build error on a ROUTED finding must not drop the alert: the
    # poller falls back to a minimal payload carrying the projected band/score.
    def boom(*a, **k):
        raise RuntimeError("evidence json exploded")

    monkeypatch.setattr(task, "build_contract_anomaly_alert", boom)
    spy["flagged"] = {"tx_ca": [_GOOD_ROW]}
    await task._contract_anomaly_tick()

    assert len(spy["delivered"]) == 1
    p = spy["delivered"][0]["payload"]
    assert p.attack_class == "contract_anomaly"
    assert p.tx_hash == "tx_ca"
    assert p.contributing_features == {}  # degraded: evidence dropped
    assert p.risk_score > 0  # projected score preserved so it pages
