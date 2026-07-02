"""contract_anomaly poller (tasks.notifications._contract_anomaly_tick).

The clustering sidecar's contract_anomaly verdict is read-time-only — it never
flows through ``on_new_scores`` — so this poller is its only notification path.
These tests pin its routing: resolve each flagged verdict, route it via the
trigger matrix, and deliver through the shared deliver-then-claim path under the
dedicated ``'contract_anomaly'`` dedup source. Per-tx failures are isolated.
"""

from datetime import datetime, timezone

import pytest

from app.analysis import contract_anomaly as ca
from app.db import clustering_queries
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
}


@pytest.fixture
def spy(monkeypatch):
    """Stub the sidecar fetch, the trigger matrix, and the delivery helper.

    ``flagged`` drives what the sidecar returns; ``dispatches`` drives whether
    the trigger matrix routes the alert anywhere.
    """
    calls = {"delivered": [], "routed": [], "flagged": {}, "dispatches": [object()]}

    async def fake_flagged(network, limit=clustering_queries._RESCUE_FETCH_CAP):
        return calls["flagged"]

    def fake_resolve_dispatch(band, attack_class):
        calls["routed"].append((band, attack_class))
        return calls["dispatches"]

    async def fake_deliver(network, tx_hash, band, payload, dispatches, source="scorer"):
        calls["delivered"].append(
            {"network": network, "tx_hash": tx_hash, "band": band,
             "payload": payload, "source": source}
        )

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
    assert d["source"] == "contract_anomaly"          # separate dedup stream
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
