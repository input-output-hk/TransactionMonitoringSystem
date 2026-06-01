"""Unit tests for dex.detect_sandwich_pattern: wallet-attacker requirement and
net-ADA-profit computation. Uses a fake ClickHouse client (canned responses
dispatched by query substring) so no database is required."""

import pytest

from app.analysis import dex

SCRIPT_POOL = "addr_test1wpool00000000000000000000000000000000000000000000000"
SCRIPT_BATCHER = "addr_test1wbatcher000000000000000000000000000000000000000000"
WALLET_ATTACKER = "addr_test1qattacker00000000000000000000000000000000000000000"
VICTIM = "addr_test1qvictim0000000000000000000000000000000000000000000000"


class FakeClient:
    """Canned responses keyed by distinctive substrings of each query."""

    def __init__(self, first_inputs, attacker_out=0, attacker_in=0, history=0):
        self._first_inputs = first_inputs   # {tx_hash: first_input_address}
        self._out = attacker_out
        self._in = attacker_in
        self._history = history

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if "SELECT DISTINCT address FROM transaction_outputs" in q:
            return [(SCRIPT_POOL,), (VICTIM,)]
        if "SELECT DISTINCT o.tx_hash" in q:
            # victim at slot 100, attacker legs straddling it.
            return [("victim", 100, 1), ("legA", 99, 1), ("legB", 101, 1)]
        if "count(DISTINCT i.tx_hash)" in q:
            return [(self._history,)]
        if "SELECT tx_hash, address FROM transaction_inputs" in q:
            return list(self._first_inputs.items())
        if "SELECT sum(amount) FROM transaction_outputs" in q:
            return [(self._out,)]
        if "sum(coalesce(o.amount, ti.amount))" in q:
            return [(self._in,)]
        return []


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
    holder = {}
    monkeypatch.setattr(dex.clickhouse, "_get_client", lambda: holder["client"])
    return holder


def test_script_attacker_cluster_excluded(_patch_client):
    # The only 2-tx cluster is a script address (pool/batcher self-interaction),
    # not a wallet attacker -> no sandwich candidate is emitted.
    _patch_client["client"] = FakeClient(
        first_inputs={"victim": VICTIM, "legA": SCRIPT_BATCHER, "legB": SCRIPT_BATCHER},
    )
    assert dex.detect_sandwich_pattern("victim", "preprod", 100) is None


def test_wallet_attacker_profit_computed(_patch_client):
    # A wallet (payment-key) cluster of 2 legs -> candidate emitted with
    # profit_b = attacker outputs - resolved attacker inputs.
    _patch_client["client"] = FakeClient(
        first_inputs={"victim": VICTIM, "legA": WALLET_ATTACKER, "legB": WALLET_ATTACKER},
        attacker_out=5_000_000, attacker_in=3_000_000, history=2,
    )
    sw = dex.detect_sandwich_pattern("victim", "preprod", 100)
    assert sw is not None
    assert sw["profit_b"] == 2_000_000.0
    assert sw["attacker_sandwich_count"] == 2
    assert sw["slot_span"] == 2


def test_wallet_attacker_negative_profit_still_reported(_patch_client):
    # Net <= 0 is still reported by the detector; suppression is the scorer's
    # job (sandwich.score returns -1 below the profit floor). The detector's
    # contract is to surface the candidate with its computed profit.
    _patch_client["client"] = FakeClient(
        first_inputs={"victim": VICTIM, "legA": WALLET_ATTACKER, "legB": WALLET_ATTACKER},
        attacker_out=1_000_000, attacker_in=1_000_000, history=0,
    )
    sw = dex.detect_sandwich_pattern("victim", "preprod", 100)
    assert sw is not None
    assert sw["profit_b"] == 0.0
