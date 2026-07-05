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

    def __init__(self, first_inputs, attacker_out=0, attacker_in=0, history=0,
                 neighbors=None, positions=None):
        self._first_inputs = first_inputs   # {tx_hash: first_input_address}
        self._out = attacker_out
        self._in = attacker_in
        self._history = history
        # Neighbour rows are (tx_hash, slot, block_index, fee). Default: attacker
        # legs straddle the victim across adjacent slots (slot_span = 2).
        self._neighbors = neighbors if neighbors is not None else [
            ("victim", 100, 0, 1), ("legA", 99, 0, 1), ("legB", 101, 0, 1),
        ]
        # (slot, block_index) per tx for the _tx_position fallback; defaults to
        # the neighbour positions.
        self._positions = positions or {n[0]: (n[1], n[2]) for n in self._neighbors}

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if "SELECT DISTINCT address FROM transaction_outputs" in q:
            return [(SCRIPT_POOL,), (VICTIM,)]
        if "SELECT DISTINCT o.tx_hash" in q:
            return self._neighbors
        if "count(DISTINCT i.tx_hash)" in q:
            return [(self._history,)]
        if "SELECT tx_hash, address FROM transaction_inputs" in q:
            return list(self._first_inputs.items())
        if "SELECT sum(amount) FROM transaction_outputs" in q:
            return [(self._out,)]
        if "sum(coalesce(o.amount, ti.amount))" in q:
            return [(self._in,)]
        if "SELECT slot, coalesce(block_index, 0) FROM transactions" in q:
            p = self._positions.get(params.get("h") if params else None)
            return [p] if p else []
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


# ---- temporal bracketing (front before victim, back after, by (slot, block_index)) ----

def test_same_block_bracketed_is_detected(_patch_client):
    """The recall win: front/victim/back in ONE slot, ordered by block_index
    (0/1/2). slot-only logic couldn't sequence these; block_index can, so a
    genuine same-block sandwich is now confirmed."""
    _patch_client["client"] = FakeClient(
        first_inputs={"victim": VICTIM, "legA": WALLET_ATTACKER, "legB": WALLET_ATTACKER},
        attacker_out=5_000_000, attacker_in=3_000_000, history=1,
        neighbors=[("legA", 100, 0, 1), ("victim", 100, 1, 1), ("legB", 100, 2, 1)],
    )
    sw = dex.detect_sandwich_pattern("victim", "preprod", 100)
    assert sw is not None
    assert sw["tx_a"] == "legA" and sw["tx_b"] == "legB"
    assert sw["slot_span"] == 0   # same block, but bracketed via block_index


def test_both_legs_before_victim_not_sandwich(_patch_client):
    # Attacker's two txs are both ordered before the victim -> co-occurrence,
    # not a front/back sandwich -> rejected.
    _patch_client["client"] = FakeClient(
        first_inputs={"victim": VICTIM, "legA": WALLET_ATTACKER, "legB": WALLET_ATTACKER},
        attacker_out=5_000_000, attacker_in=3_000_000,
        neighbors=[("legA", 98, 0, 1), ("legB", 99, 0, 1), ("victim", 100, 0, 1)],
    )
    assert dex.detect_sandwich_pattern("victim", "preprod", 100) is None


def test_both_legs_after_victim_not_sandwich(_patch_client):
    _patch_client["client"] = FakeClient(
        first_inputs={"victim": VICTIM, "legA": WALLET_ATTACKER, "legB": WALLET_ATTACKER},
        attacker_out=5_000_000, attacker_in=3_000_000,
        neighbors=[("victim", 100, 0, 1), ("legA", 101, 0, 1), ("legB", 102, 0, 1)],
    )
    assert dex.detect_sandwich_pattern("victim", "preprod", 100) is None


def test_same_block_legs_one_side_not_sandwich(_patch_client):
    # Same slot, but both attacker legs sit AFTER the victim by block_index.
    _patch_client["client"] = FakeClient(
        first_inputs={"victim": VICTIM, "legA": WALLET_ATTACKER, "legB": WALLET_ATTACKER},
        attacker_out=5_000_000, attacker_in=3_000_000,
        neighbors=[("victim", 100, 0, 1), ("legA", 100, 1, 1), ("legB", 100, 2, 1)],
    )
    assert dex.detect_sandwich_pattern("victim", "preprod", 100) is None


def test_victim_outside_neighbour_window_uses_position_fallback(_patch_client):
    # Victim not present in the (capped) neighbour rows; its (slot, block_index)
    # is resolved via the _tx_position fallback so bracketing still works.
    _patch_client["client"] = FakeClient(
        first_inputs={"legA": WALLET_ATTACKER, "legB": WALLET_ATTACKER, "other": VICTIM},
        attacker_out=5_000_000, attacker_in=3_000_000, history=1,
        neighbors=[("legA", 99, 0, 1), ("legB", 101, 0, 1), ("other", 100, 0, 1)],
        positions={"victim": (100, 0)},   # fallback lookup for the victim
    )
    sw = dex.detect_sandwich_pattern("victim", "preprod", 100)
    assert sw is not None
    assert sw["tx_a"] == "legA" and sw["tx_b"] == "legB"


# ---- _bracketing_legs (pure: no DB) ----

def test_bracketing_legs_straddles_victim():
    pos = {"a": (100, 0), "v": (100, 1), "b": (100, 2)}
    assert dex._bracketing_legs(["a", "b"], pos, victim_pos=(100, 1)) == ("a", "b", 0)


def test_bracketing_legs_picks_closest_on_each_side():
    pos = {"a0": (98, 0), "a1": (99, 0), "v": (100, 0), "b1": (101, 0), "b2": (102, 0)}
    # closest front = a1 (99), closest back = b1 (101), span = 2
    assert dex._bracketing_legs(["a0", "a1", "b1", "b2"], pos, (100, 0)) == ("a1", "b1", 2)


def test_bracketing_legs_no_straddle_returns_none():
    pos = {"a": (98, 0), "b": (99, 0), "v": (100, 0)}
    assert dex._bracketing_legs(["a", "b"], pos, (100, 0)) is None


# ---- highest-profit selection across multiple bracketing clusters ----

class _ProfitByAddrClient(FakeClient):
    """FakeClient that returns attacker out/in keyed by the cluster address in
    the query params, so distinct candidate clusters get distinct profits."""

    def __init__(self, first_inputs, profit_by_addr, neighbors=None):
        super().__init__(first_inputs, neighbors=neighbors)
        self._profit_by_addr = profit_by_addr  # {addr: (out, in)}

    def execute(self, query, params=None):
        q = " ".join(query.split())
        addr = (params or {}).get("addr")
        if addr is not None and "SELECT sum(amount) FROM transaction_outputs" in q:
            return [(self._profit_by_addr.get(addr, (0, 0))[0],)]
        if addr is not None and "sum(coalesce(o.amount, ti.amount))" in q:
            return [(self._profit_by_addr.get(addr, (0, 0))[1],)]
        return super().execute(query, params)


def test_highest_profit_cluster_wins_over_benign_bracket(_patch_client):
    # Two wallet clusters both straddle the victim. BENIGN nets 0 and sorts
    # FIRST alphabetically; PROFIT nets +4 ADA and sorts last. The old
    # "return the first bracketing cluster" logic returned BENIGN (which the
    # scorer's profit floor then suppresses), masking the real sandwich. The
    # detector must now return the highest-profit candidate.
    BENIGN = "addr_test1qaaabenign0000000000000000000000000000000000000000000"
    PROFIT = "addr_test1qzzzprofit0000000000000000000000000000000000000000000"
    neighbors = [
        ("victim", 100, 0, 1),
        ("benA", 99, 0, 1), ("benB", 101, 0, 1),
        ("proA", 98, 0, 1), ("proB", 102, 0, 1),
    ]
    _patch_client["client"] = _ProfitByAddrClient(
        first_inputs={
            "victim": VICTIM,
            "benA": BENIGN, "benB": BENIGN,
            "proA": PROFIT, "proB": PROFIT,
        },
        profit_by_addr={
            BENIGN: (1_000_000, 1_000_000),   # net 0
            PROFIT: (5_000_000, 1_000_000),   # net +4 ADA
        },
        neighbors=neighbors,
    )
    sw = dex.detect_sandwich_pattern("victim", "preprod", 100)
    assert sw is not None
    assert sw["profit_b"] == 4_000_000.0  # PROFIT cluster, not the benign one
    assert {sw["tx_a"], sw["tx_b"]} == {"proA", "proB"}


def test_sandwich_selection_is_deterministic(_patch_client):
    # Same input twice must yield the same candidate (sorted cluster scan).
    BENIGN = "addr_test1qaaabenign0000000000000000000000000000000000000000000"
    PROFIT = "addr_test1qzzzprofit0000000000000000000000000000000000000000000"
    neighbors = [
        ("victim", 100, 0, 1),
        ("benA", 99, 0, 1), ("benB", 101, 0, 1),
        ("proA", 98, 0, 1), ("proB", 102, 0, 1),
    ]

    def build():
        return _ProfitByAddrClient(
            first_inputs={"victim": VICTIM, "benA": BENIGN, "benB": BENIGN,
                          "proA": PROFIT, "proB": PROFIT},
            profit_by_addr={BENIGN: (1_000_000, 1_000_000),
                            PROFIT: (5_000_000, 1_000_000)},
            neighbors=neighbors,
        )

    _patch_client["client"] = build()
    a = dex.detect_sandwich_pattern("victim", "preprod", 100)
    _patch_client["client"] = build()
    b = dex.detect_sandwich_pattern("victim", "preprod", 100)
    assert a == b
