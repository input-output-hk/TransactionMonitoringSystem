"""Live-ClickHouse tests for the circular scorer's forward BFS.

The hermetic graph tests stub ``client.execute`` with canned rows, so they
assert the Python BFS logic and never see the SQL. That leaves the hop query
itself untested: a shape change that silently drops legs keeps the hermetic
suite green and loses real cycles in production. This tier plants known cycles
in live ClickHouse and requires detect_cycle to find them.

Cycles here are 3 legs, not 2, because ``circular.cycle.min_length`` is 3: a
2-hop round-trip (A -> script -> A) is a deposit/withdraw and CircularScorer
discards it, so a 2-leg plant would assert a shape that can never alert. Each
test asserts through ``CircularScorer.gate`` for that reason.

Requires TMS_LIVE_DB_TESTS=1 (see conftest).
"""

import uuid
from datetime import UTC, datetime

from app.analysis import graph
from app.analysis.scorers.circular import (
    FEE_TOLERANCE_MULTIPLIER,
    CircularScorer,
    _estimate_fee_ratio,
)
from app.models.transaction import (
    NormalizedTransaction,
    TransactionInput,
    TransactionOutput,
)

from .conftest import LIVE_NETWORK

# Legs land a few slots apart so hop ordering is unambiguous while staying well
# inside circular.cycle.max_age_slots (the 24h forward horizon).
_LEG_SLOT_GAP = 10
_ORIGIN_SLOT = 2_000_000

# A 3-leg cycle losing only ~2% end to end, comfortably inside the gate's
# fee tolerance (per_hop_fee_estimate 0.02 * 3 legs * fee_tolerance_multiplier
# 4.0 = 0.24 max net_loss_ratio).
_LEG_1 = 10_000_000
_LEG_2 = 9_900_000
_LEG_3 = 9_800_000
# A min-UTxO-sized second output back to the origin on the closing leg. Routine
# on Cardano (token dust, change) and the reason the hop query orders amount
# DESC: see test_dust_output_on_the_closing_leg_does_not_mask_the_cycle.
_DUST = 1_000_000
# Every plant here closes after 3 legs; the gate's loss ceiling for that
# length, from the scorer's own constants so a retune cannot leave a stale copy.
_CYCLE_LEGS = 3
_GATE_MAX_NET_LOSS = _estimate_fee_ratio(_CYCLE_LEGS) * FEE_TOLERANCE_MULTIPLIER


def _tx(tx_hash, slot, from_addrs, outputs):
    """One leg of a cycle: spends from `from_addrs`, pays `outputs` [(addr, amount)]."""
    total = sum(amount for _, amount in outputs)
    return NormalizedTransaction(
        tx_hash=tx_hash,
        network=LIVE_NETWORK,
        slot=slot,
        block_height=slot,
        block_hash="ab" * 32,
        block_index=0,
        timestamp=datetime.now(UTC),
        fee=200_000,
        inputs=[
            TransactionInput(
                tx_hash="cd" * 32, index=i, address=addr, amount=total // len(from_addrs)
            )
            for i, addr in enumerate(from_addrs)
        ],
        outputs=[TransactionOutput(address=addr, amount=amount) for addr, amount in outputs],
        input_count=len(from_addrs),
        output_count=len(outputs),
        total_input_value=total,
        total_output_value=total,
        addresses=list(from_addrs) + [addr for addr, _ in outputs],
    )


def _plant(ch, closing_outputs, closing_slot=None, origin_count=1):
    """Plant origin -> B -> C -> origin and return (origin_tx_hash, origins).

    The origin transaction spends from `origin_count` addresses of one wallet,
    named so they sort in list order. `closing_outputs` is a factory taking that
    list and returning the final leg's outputs, so a test varies only how the
    cycle closes.
    """
    run = uuid.uuid4().hex[:12]
    origins = [f"addr_test1qqo{i}_{run}" for i in range(origin_count)]
    addr_b, addr_c = (f"addr_test1qq{leg}_{run}" for leg in ("b", "c"))
    tx_ab, tx_bc, tx_ca = (uuid.uuid4().hex * 2 for _ in range(3))

    ch.insert_transactions_batch(
        [
            _tx(tx_ab, _ORIGIN_SLOT, origins, [(addr_b, _LEG_1)]),
            _tx(tx_bc, _ORIGIN_SLOT + _LEG_SLOT_GAP, [addr_b], [(addr_c, _LEG_2)]),
            _tx(
                tx_ca,
                closing_slot if closing_slot is not None else _ORIGIN_SLOT + 2 * _LEG_SLOT_GAP,
                [addr_c],
                closing_outputs(origins),
            ),
        ]
    )
    return tx_ab, origins


class TestForwardBfsFindsAPlantedCycle:
    def test_three_hop_cycle_fires_the_scorer_gate(self, ch):
        """A -> B -> C -> A must be found AND must clear CircularScorer.gate.

        This is the attack-must-fire case for the BFS. If the hop query stops
        returning a closing leg, recall for circular layering is gone and the
        hermetic suite cannot tell: it mocks the query away.
        """
        tx_ab, origins = _plant(ch, lambda o: [(o[0], _LEG_3)])

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle is not None, "planted A->B->C->A cycle was not detected"
        assert cycle["cycle_length"] == 3
        assert origins[0] in cycle["addresses"]
        assert CircularScorer().gate({"cycle": cycle}), (
            f"real cycle rejected by the gate: net_loss_ratio={cycle['net_loss_ratio']}"
        )

    def test_dust_output_on_the_closing_leg_does_not_mask_the_cycle(self, ch):
        """The closing leg paying the origin twice must not silence the finding.

        A min-UTxO dust output beside the real repayment to the same address was
        once taken as the repayment whenever it sorted first, pushing
        net_loss_ratio to ~0.9 so the gate rejected a genuine cycle. The amount
        returned is now the closing transaction's total to the origin set, so
        this holds whatever order the hop query returns its rows in.
        """
        tx_ab, _ = _plant(ch, lambda o: [(o[0], _LEG_3), (o[0], _DUST)])

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle is not None
        assert cycle["net_loss_ratio"] < _GATE_MAX_NET_LOSS, (
            f"dust output was taken as the repayment: net_loss_ratio={cycle['net_loss_ratio']}"
        )
        assert CircularScorer().gate({"cycle": cycle})

    def test_dust_to_a_second_origin_address_does_not_mask_the_repayment(self, ch):
        """Dust to one origin address must not stand in for the repayment to another.

        The origin wallet spent from two addresses. The closing leg sends dust
        to the one that sorts first and the repayment to the other, so the hop
        query's first origin-paying row is the dust whatever the amount order.
        """
        tx_ab, _ = _plant(ch, lambda o: [(o[0], _DUST), (o[1], _LEG_3 - _DUST)], origin_count=2)

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle is not None
        assert cycle["net_loss_ratio"] < _GATE_MAX_NET_LOSS, (
            f"dust stood in for the repayment: net_loss_ratio={cycle['net_loss_ratio']}"
        )
        assert CircularScorer().gate({"cycle": cycle})

    def test_repayment_split_into_equal_outputs_counts_in_full(self, ch):
        """Equal outputs to one origin address must all count.

        The hop query is DISTINCT on (tx_hash, address, amount, slot), so real
        ClickHouse returns the two halves as ONE row. The total must still
        include both, or half the repayment reads as lost and the gate rejects.
        """
        half = _LEG_3 // 2
        tx_ab, _ = _plant(ch, lambda o: [(o[0], half), (o[0], half)])

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle is not None
        assert cycle["net_loss_ratio"] < _GATE_MAX_NET_LOSS, (
            f"half the repayment was dropped: net_loss_ratio={cycle['net_loss_ratio']}"
        )
        assert CircularScorer().gate({"cycle": cycle})


class TestForwardBfsHorizon:
    def test_leg_just_inside_the_horizon_is_still_found(self, ch):
        """Positive control for the horizon bound.

        Without this, the beyond-horizon test below passes for any breakage that
        makes the hop query return nothing at all.
        """
        inside = _ORIGIN_SLOT + graph._MAX_AGE_SLOTS - _LEG_SLOT_GAP
        tx_ab, _ = _plant(ch, lambda o: [(o[0], _LEG_3)], closing_slot=inside)

        assert graph.detect_cycle(tx_ab, LIVE_NETWORK) is not None

    def test_leg_outside_the_horizon_is_not_a_cycle(self, ch):
        """The return leg beyond max_age_slots must not close a cycle.

        Guards the other direction: the slot window moved into a subquery, so a
        rewrite that dropped the bound would invent cycles across unrelated
        reuses of an address months apart.
        """
        beyond = _ORIGIN_SLOT + graph._MAX_AGE_SLOTS + _LEG_SLOT_GAP
        tx_ab, _ = _plant(ch, lambda o: [(o[0], _LEG_3)], closing_slot=beyond)

        assert graph.detect_cycle(tx_ab, LIVE_NETWORK) is None
