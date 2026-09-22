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
from app.analysis.scorers.circular import CircularScorer
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


def _tx(tx_hash, slot, from_addr, outputs):
    """One leg of a cycle: spends `from_addr`, pays `outputs` [(addr, amount)]."""
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
        inputs=[TransactionInput(tx_hash="cd" * 32, index=0, address=from_addr, amount=total)],
        outputs=[TransactionOutput(address=addr, amount=amount) for addr, amount in outputs],
        input_count=1,
        output_count=len(outputs),
        total_input_value=total,
        total_output_value=total,
        addresses=[from_addr] + [addr for addr, _ in outputs],
    )


def _plant(ch, closing_outputs, closing_slot=None):
    """Plant A -> B -> C -> A and return (origin_tx_hash, addr_a).

    `closing_outputs` is a factory taking addr_a and returning the final leg's
    output list, so a test can vary only how the cycle closes.
    """
    run = uuid.uuid4().hex[:12]
    addr_a, addr_b, addr_c = (f"addr_test1qq{leg}_{run}" for leg in ("a", "b", "c"))
    tx_ab, tx_bc, tx_ca = (uuid.uuid4().hex * 2 for _ in range(3))

    ch.insert_transactions_batch(
        [
            _tx(tx_ab, _ORIGIN_SLOT, addr_a, [(addr_b, _LEG_1)]),
            _tx(tx_bc, _ORIGIN_SLOT + _LEG_SLOT_GAP, addr_b, [(addr_c, _LEG_2)]),
            _tx(
                tx_ca,
                closing_slot if closing_slot is not None else _ORIGIN_SLOT + 2 * _LEG_SLOT_GAP,
                addr_c,
                closing_outputs(addr_a),
            ),
        ]
    )
    return tx_ab, addr_a


class TestForwardBfsFindsAPlantedCycle:
    def test_three_hop_cycle_fires_the_scorer_gate(self, ch):
        """A -> B -> C -> A must be found AND must clear CircularScorer.gate.

        This is the attack-must-fire case for the BFS. If the hop query stops
        returning a closing leg, recall for circular layering is gone and the
        hermetic suite cannot tell: it mocks the query away.
        """
        tx_ab, addr_a = _plant(ch, lambda a: [(a, _LEG_3)])

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle is not None, "planted A->B->C->A cycle was not detected"
        assert cycle["cycle_length"] == 3
        assert addr_a in cycle["addresses"]
        assert CircularScorer().gate({"cycle": cycle}), (
            f"real cycle rejected by the gate: net_loss_ratio={cycle['net_loss_ratio']}"
        )

    def test_dust_output_on_the_closing_leg_does_not_mask_the_cycle(self, ch):
        """The closing leg paying the origin twice must not silence the finding.

        The BFS returns on the FIRST row paying an origin address and uses that
        row's amount as final_amount, which drives net_loss_ratio and the gate.
        With the hop query ordering amount ASC, a min-UTxO dust output alongside
        the real repayment surfaces first, net_loss_ratio goes to ~0.9 and the
        gate rejects a genuine cycle. Ordering amount DESC is what keeps this
        test green, so it fails if that direction is ever flipped back.
        """
        tx_ab, addr_a = _plant(ch, lambda a: [(a, _LEG_3), (a, _DUST)])

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle is not None
        assert cycle["net_loss_ratio"] < 0.24, (
            f"dust output was taken as the repayment: net_loss_ratio={cycle['net_loss_ratio']}"
        )
        assert CircularScorer().gate({"cycle": cycle})


class TestForwardBfsHorizon:
    def test_leg_just_inside_the_horizon_is_still_found(self, ch):
        """Positive control for the horizon bound.

        Without this, the beyond-horizon test below passes for any breakage that
        makes the hop query return nothing at all.
        """
        inside = _ORIGIN_SLOT + graph._MAX_AGE_SLOTS - _LEG_SLOT_GAP
        tx_ab, _ = _plant(ch, lambda a: [(a, _LEG_3)], closing_slot=inside)

        assert graph.detect_cycle(tx_ab, LIVE_NETWORK) is not None

    def test_leg_outside_the_horizon_is_not_a_cycle(self, ch):
        """The return leg beyond max_age_slots must not close a cycle.

        Guards the other direction: the slot window moved into a subquery, so a
        rewrite that dropped the bound would invent cycles across unrelated
        reuses of an address months apart.
        """
        beyond = _ORIGIN_SLOT + graph._MAX_AGE_SLOTS + _LEG_SLOT_GAP
        tx_ab, _ = _plant(ch, lambda a: [(a, _LEG_3)], closing_slot=beyond)

        assert graph.detect_cycle(tx_ab, LIVE_NETWORK) is None
