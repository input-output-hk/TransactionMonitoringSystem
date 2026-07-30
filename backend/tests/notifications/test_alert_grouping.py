"""Alert grouping: collapse a burst of one-situation findings into one alert.

The incident this exists for: on mainnet 2026-07-26 a single script re-spent a
fixed ~12.5 KB inline datum 106 times in 67 minutes, and because every spend is
a distinct tx_hash the per-transaction dedup never fired. That was 106 Critical
pages, 77% of the window's entire alerting volume, for one situation.

Grouping bounds NOTIFICATIONS only, never findings, so these tests pin both
halves: the collapse actually happens, and the two recall guards (band
escalation and window expiry) let a real change through immediately.
"""

import pytest

from app import notifications
from app.db import postgres
from app.notifications import dispatcher, grouping
from app.utils.bech32 import payment_credential_or_raw

# Two bech32-decodable preprod script addresses with distinct payment
# credentials, the same pair the large_datum scorer tests use.
SCRIPT_A = "addr_test1wzpzd7k0lkpkr3kh460ll6c88r4p5nhqehu3gjtc359a2ds9qpd7c"
SCRIPT_B = "addr_test1wpnlxv2xv9a9ucvnvzqakwepzl9ltx7jzgm53av2e9ncv4sysemm8"


def _evidence(address, attack_class="large_datum"):
    return {attack_class: {"target_script_address": address}}


class TestGroupKey:
    def test_large_datum_groups_on_the_target_script(self):
        key = grouping.group_key("large_datum", _evidence(SCRIPT_A))
        assert key is not None
        assert key.startswith("large_datum:")

    def test_distinct_scripts_get_distinct_keys(self):
        a = grouping.group_key("large_datum", _evidence(SCRIPT_A))
        b = grouping.group_key("large_datum", _evidence(SCRIPT_B))
        assert a != b

    def test_key_is_the_payment_credential_not_the_raw_address(self):
        # The identity has to be the payment credential so that one validator
        # reached through different stake credentials is one group: treating
        # those as separate identities would let the burst through. The
        # cross-variant behaviour itself belongs to payment_credential_or_raw
        # (see tests/analysis/scorers/test_payment_credential.py); what this
        # pins is that grouping delegates to it rather than keying on the
        # address it was handed.
        key = grouping.group_key("large_datum", _evidence(SCRIPT_A))
        assert key == f"large_datum:{payment_credential_or_raw(SCRIPT_A)}"
        assert SCRIPT_A not in key

    def test_undecodable_address_still_groups_on_the_raw_value(self):
        # payment_credential_or_raw falls back to the raw address on a decode
        # failure, so an unparseable address still collapses its own burst
        # rather than dropping out of grouping entirely.
        key = grouping.group_key("large_datum", _evidence("not-a-bech32-address"))
        assert key == "large_datum:not-a-bech32-address"

    def test_ungrouped_class_returns_none(self):
        # The bound on this feature: phishing findings sharing a sender are NOT
        # one situation, because each transaction is a different victim, so the
        # class must not be grouped. Guards against someone widening the map
        # without the evidence to justify it.
        assert grouping.group_key("phishing", _evidence(SCRIPT_A, "phishing")) is None

    @pytest.mark.parametrize(
        "evidence",
        [
            None,
            {},
            "not-a-dict",
            {"large_datum": None},
            {"large_datum": {}},
            {"large_datum": {"target_script_address": ""}},
            {"large_datum": {"target_script_address": 42}},
            {"other_class": {"target_script_address": SCRIPT_A}},
        ],
    )
    def test_unusable_evidence_fails_open_to_ungrouped(self, evidence):
        # Failing open costs a duplicate alert; failing closed would cost the
        # alert itself, so None is the only safe answer when the identity
        # cannot be established.
        assert grouping.group_key("large_datum", evidence) is None


@pytest.fixture
def spy(monkeypatch):
    """Stub both dedup ledgers and the dispatcher, recording every call."""
    calls = {
        "group_checks": [],
        "group_claims": [],
        "claims": [],
        "dispatch": [],
        "group_suppresses": False,
    }

    async def fake_already(network, tx_hash, band, source="scorer"):
        return False  # per-tx dedup never fires here: every tx_hash is new

    async def fake_claim(network, tx_hash, band, source="scorer"):
        calls["claims"].append((tx_hash, band))
        return True

    async def fake_group_check(network, group_key, band, window_minutes, source="scorer"):
        calls["group_checks"].append((group_key, band, window_minutes))
        return calls["group_suppresses"]

    async def fake_group_claim(network, group_key, band, source="scorer"):
        calls["group_claims"].append((group_key, band))

    async def fake_dispatch(payload, dispatches, attachments=None):
        calls["dispatch"].append(payload)
        return True

    monkeypatch.setattr(postgres, "already_notified", fake_already)
    monkeypatch.setattr(postgres, "claim_notification", fake_claim)
    monkeypatch.setattr(postgres, "already_notified_group", fake_group_check)
    monkeypatch.setattr(postgres, "claim_notification_group", fake_group_claim)
    monkeypatch.setattr(dispatcher, "dispatch", fake_dispatch)
    return calls


async def _deliver(group=None, tx_hash="tx1", band="Critical"):
    return await notifications._deliver_with_dedup(
        "preprod", tx_hash, band, object(), [object()], group=group
    )


class TestDeliveryPath:
    pytestmark = pytest.mark.asyncio

    async def test_ungrouped_alert_never_touches_the_group_ledger(self, spy):
        assert await _deliver(group=None) == notifications.DELIVER_SENT
        assert spy["group_checks"] == []
        assert spy["group_claims"] == []
        assert len(spy["dispatch"]) == 1

    async def test_first_of_a_group_delivers_and_claims(self, spy):
        assert await _deliver(group="large_datum:abc") == notifications.DELIVER_SENT
        assert len(spy["dispatch"]) == 1
        assert spy["group_claims"] == [("large_datum:abc", "Critical")]

    async def test_repeat_within_the_window_is_collapsed(self, spy):
        spy["group_suppresses"] = True
        assert await _deliver(group="large_datum:abc", tx_hash="tx2") == (
            notifications.DELIVER_DUPLICATE
        )
        # Nothing on the wire, and no claim: the open window is untouched, so
        # its expiry stays measured from the alert that was actually sent.
        assert spy["dispatch"] == []
        assert spy["group_claims"] == []

    async def test_collapsed_alert_records_no_per_tx_claim_either(self, spy):
        # A collapsed tx must stay unclaimed at the per-tx level too, so that
        # once the window expires the next re-score can still notify for it.
        spy["group_suppresses"] = True
        await _deliver(group="large_datum:abc", tx_hash="tx3")
        assert spy["claims"] == []

    async def test_group_check_receives_the_configured_window(self, spy, monkeypatch):
        monkeypatch.setattr(
            notifications.settings, "NOTIFY_GROUP_WINDOW_MINUTES", 15, raising=False
        )
        await _deliver(group="large_datum:abc")
        assert spy["group_checks"] == [("large_datum:abc", "Critical", 15)]

    async def test_zero_window_skips_the_group_ledger_entirely(self, spy, monkeypatch):
        # The disable switch has to hold on BOTH sides: not just "never
        # suppress", but never claim either, or an operator who turned grouping
        # off would still accumulate ledger rows nothing ever reads.
        monkeypatch.setattr(notifications.settings, "NOTIFY_GROUP_WINDOW_MINUTES", 0, raising=False)
        assert await _deliver(group="large_datum:abc") == notifications.DELIVER_SENT
        assert spy["group_checks"] == []
        assert spy["group_claims"] == []
        assert len(spy["dispatch"]) == 1

    async def test_group_ledger_failure_delivers_anyway(self, spy, monkeypatch):
        # Recall-first: an unavailable ledger must never suppress an alert.
        async def boom(*a, **k):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(postgres, "already_notified_group", boom)
        assert await _deliver(group="large_datum:abc") == notifications.DELIVER_SENT
        assert len(spy["dispatch"]) == 1

    async def test_failed_delivery_leaves_the_group_unclaimed(self, spy, monkeypatch):
        # Deliver-then-claim, same as the per-tx ledger: a total-channel failure
        # must not open a window that would then suppress the retry.
        async def no_delivery(payload, dispatches, attachments=None):
            return False

        monkeypatch.setattr(dispatcher, "dispatch", no_delivery)
        assert await _deliver(group="large_datum:abc") == notifications.DELIVER_FAILED
        assert spy["group_claims"] == []


class TestGroupLedgerSemantics:
    """The recall guards live in already_notified_group's query. These pin the
    argument shape and the disable switch without needing Postgres; the SQL
    itself is exercised by the live-DB tier."""

    pytestmark = pytest.mark.asyncio

    async def test_zero_window_disables_grouping(self):
        # No DB call at all, so this is safe to assert without a connection: a
        # zero window must short-circuit before touching the ledger.
        assert (
            await postgres.already_notified_group("preprod", "large_datum:abc", "Critical", 0)
            is False
        )

    async def test_unknown_band_does_not_suppress(self):
        assert (
            await postgres.already_notified_group("preprod", "large_datum:abc", "NotABand", 60)
            is False
        )
