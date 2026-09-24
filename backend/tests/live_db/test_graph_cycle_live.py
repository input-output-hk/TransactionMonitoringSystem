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

import pytest

from app.analysis import graph
from app.analysis.normalise import BAND_HIGH_THRESHOLD
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
# Change the closing leg sends back to the wallet beside the repayment, 20% of
# the cycled amount (routine when one wallet controls every leg).
_CHANGE = 2_000_000
# Every plant here closes after 3 legs; the gate's loss ceiling for that
# length, from the scorer's own constants so a retune cannot leave a stale copy.
_CYCLE_LEGS = 3
_GATE_MAX_NET_LOSS = _estimate_fee_ratio(_CYCLE_LEGS) * FEE_TOLERANCE_MULTIPLIER


def _tx(tx_hash, slot, from_addrs, outputs):
    """One leg of a cycle: spends from `from_addrs`, pays `outputs` [(addr, amount)].

    `addresses` lists the spent-from addresses as well as the paid ones, as
    input enrichment does before every production insert. The hop query finds
    spends through address_transactions, which is built from this list.
    """
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


def _ring(origin_count=1):
    """Fresh (origins, B, C) addresses for one planted ring, unique per call."""
    run = uuid.uuid4().hex[:12]
    origins = [f"addr_test1qqo{i}_{run}" for i in range(origin_count)]
    addr_b, addr_c = (f"addr_test1qq{leg}_{run}" for leg in ("b", "c"))
    return origins, addr_b, addr_c


def _plant(
    ch,
    closing_outputs,
    closing_slot=None,
    origin_count=1,
    ring=None,
    origin_slot=_ORIGIN_SLOT,
    leg_gap=_LEG_SLOT_GAP,
    leg_amounts=(_LEG_1, _LEG_2),
    extra_origin_outputs=(),
    replay_closing=False,
    decoys=None,
):
    """Plant origin -> B -> C -> origin and return (origin_tx_hash, origins).

    The origin transaction spends from `origin_count` addresses of one wallet,
    named so they sort in list order. `closing_outputs` is a factory taking that
    list and returning the final leg's outputs, so a test varies only how the
    cycle closes. Passing the same `ring` (from _ring) twice replays the cycle
    through the same addresses, from `origin_slot` on. `replay_closing` inserts
    the closing transaction a second time, as a replayed block does, leaving
    duplicate rows until a merge. `decoys`, if given, takes (origins, addr_b)
    and returns extra transactions planted alongside the cycle.
    """
    origins, addr_b, addr_c = ring or _ring(origin_count)
    tx_ab, tx_bc, tx_ca = (uuid.uuid4().hex * 2 for _ in range(3))
    leg_1, leg_2 = leg_amounts

    ch.insert_transactions_batch(
        [
            _tx(tx_ab, origin_slot, origins, [(addr_b, leg_1), *extra_origin_outputs]),
            _tx(tx_bc, origin_slot + leg_gap, [addr_b], [(addr_c, leg_2)]),
            *(decoys(origins, addr_b) if decoys else []),
        ]
    )
    closing = _tx(
        tx_ca,
        closing_slot if closing_slot is not None else origin_slot + 2 * leg_gap,
        [addr_c],
        closing_outputs(origins),
    )
    for _ in range(2 if replay_closing else 1):
        ch.insert_transactions_batch([closing])
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

    def test_a_payment_into_the_frontier_is_not_a_spend_from_it(self, ch):
        """Only a transaction SPENDING from a frontier address extends the BFS.

        The hop query prefilters through address_transactions, which lists
        every transaction touching an address, including ones that only pay
        it. Here an unrelated wallet pays B and the origin in one transaction,
        landing after the origin leg and before B's real spend. Taken as B's
        spend, it would close a 2-leg "cycle" first; the BFS stops at the first
        origin-paying row and the gate discards 2-leg cycles, so the real 3-leg
        cycle would go unreported. Confirming each candidate against
        transaction_inputs is what keeps it out.
        """

        def payer_into_b(origins, addr_b):
            return [
                _tx(
                    uuid.uuid4().hex * 2,
                    _ORIGIN_SLOT + _LEG_SLOT_GAP // 2,
                    [f"addr_test1qqx_{uuid.uuid4().hex[:12]}"],
                    [(addr_b, _DUST), (origins[0], _LEG_3)],
                )
            ]

        tx_ab, origins = _plant(ch, lambda o: [(o[0], _LEG_3)], decoys=payer_into_b)

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle is not None, "a payment into B masked the real cycle"
        assert cycle["cycle_length"] == _CYCLE_LEGS
        assert CircularScorer().gate({"cycle": cycle})

    @pytest.mark.parametrize(
        "closing",
        [
            lambda o: [(o[0], _LEG_3), (o[1], _CHANGE)],
            lambda o: [(o[0], _LEG_3 // 2), (o[0], _LEG_3 // 2), (o[0], _CHANGE)],
        ],
        ids=["change-to-second-origin-address", "split-repayment-and-change-to-the-same-address"],
    )
    def test_change_back_to_the_wallet_does_not_lower_amount_similarity(self, ch, closing):
        """Change back to the origin wallet is not a mismatch.

        The closing leg's total to the origin set is what the loss gate needs,
        but read as the closing hop's amount it turned routine change into an
        uneven cycle and dropped a real one out of the alert band. The cycle
        must measure exactly as it does without the change, also when the
        repayment is split into equal outputs (which the hop query's DISTINCT
        collapses) beside change to the same address.
        """
        tx_plain, _ = _plant(ch, lambda o: [(o[0], _LEG_3)], origin_count=2)
        tx_change, _ = _plant(ch, closing, origin_count=2)

        plain = graph.detect_cycle(tx_plain, LIVE_NETWORK)
        with_change = graph.detect_cycle(tx_change, LIVE_NETWORK)

        assert plain is not None and with_change is not None
        assert with_change["amount_similarity"] == plain["amount_similarity"]
        assert CircularScorer().gate({"cycle": with_change})

    def test_a_replayed_closing_transaction_is_counted_once(self, ch):
        """A closing leg inserted twice must not count its repayment twice.

        A replayed block re-inserts its transactions, and the duplicate rows
        stay until ClickHouse merges them. Read without FINAL, the repayment
        doubles, the loss reads as zero and the similarity as uneven. The
        measured loss must be the single-count one.
        """
        tx_ab, _ = _plant(ch, lambda o: [(o[0], _LEG_3)], replay_closing=True)

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle is not None
        assert cycle["net_loss_ratio"] == round((_LEG_1 - _LEG_3) / _LEG_1, 4)


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


# A second pass of a ring starts well after the first has closed, so the hop
# query's forward window from the second origin transaction cannot pick up the
# first ring's legs, yet stays inside circular.recurrence_window_days.
_REPLAY_OFFSET_SLOTS = 1_000
# How far inside or outside the history window's edge a planted prior sits: a
# minute of chain time, far above the one-slot resolution of the bound.
_WINDOW_EDGE_SLOTS = 60
# A prior circular score just below the High band: pooled for the recycled
# share, never counted as recurrence.
_BELOW_HIGH = BAND_HIGH_THRESHOLD - 1
# Legs a block or more apart: the speed and timing axes read 0 at this gap.
_SLOW_LEG_SLOT_GAP = 25
# Added to every leg so no amount is a whole number of ADA.
_ODD_LOVELACE = 170_001
# The schema convention for "the scorer produced no finding".
_NO_FINDING = -1.0


def _naive_utc_now() -> datetime:
    # The ClickHouse driver expects naive UTC datetimes for DateTime columns.
    return datetime.now(UTC).replace(tzinfo=None)


def _store_score(ch, tx_hash, circular, evidence, network=LIVE_NETWORK):
    """Persist a circular score the way the engine does: the class's evidence
    under its name in the evidence blob, which the circular_* columns derive from."""
    ch.insert_class_scores(
        [
            {
                "tx_hash": tx_hash,
                "network": network,
                "circular": circular,
                "max_score": max(circular, 0.0),
                "max_class": "circular",
                "risk_band": "Moderate",
                "evidence": {"circular": evidence},
                "analysis_version": "live-db-test",
                "analyzed_at": _naive_utc_now(),
            }
        ]
    )


def _score_and_store(ch, tx_hash):
    """Detect, score and store one cycle as the engine would, suppressed or not."""
    cycle = graph.detect_cycle(tx_hash, LIVE_NETWORK)
    assert cycle is not None
    result = CircularScorer().score({"cycle": cycle, "network": LIVE_NETWORK})
    _store_score(ch, tx_hash, result.score, result.evidence)
    return cycle, result


def _prior(first_slot, intermediaries, origins):
    """Evidence of an earlier cycle as the scorer stores it, reduced to what the
    history read uses."""
    return {"first_slot": first_slot, "intermediaries": intermediaries, "origin_keys": origins}


class TestCycleHistoryFromStoredScores:
    """The window axes read earlier cycles back from tx_class_scores through
    the circular_* columns materialized from the stored evidence. A mocked
    client proves neither the column expressions nor the read's filters."""

    def test_a_replayed_ring_reads_the_first_pass_from_its_stored_score(self, ch):
        """The second pass through the same intermediaries must see the first.

        The first ring is scored for real and stored the way the engine stores
        it, so this covers the evidence the scorer writes, the columns
        ClickHouse derives from it, and the read that pools it.
        """
        ring = _ring()
        first_tx, _ = _plant(ch, lambda o: [(o[0], _LEG_3)], ring=ring)
        first, _ = _score_and_store(ch, first_tx)
        assert first["prior_cycles_in_window"] == 0

        again_tx, _ = _plant(
            ch,
            lambda o: [(o[0], _LEG_3)],
            ring=ring,
            origin_slot=_ORIGIN_SLOT + _REPLAY_OFFSET_SLOTS,
        )
        again = graph.detect_cycle(again_tx, LIVE_NETWORK)

        assert again is not None
        assert again["intermediaries"] == [ring[1], ring[2]]
        assert again["prior_cycles_in_window"] == 1
        assert again["recycled_share"] == 1.0

    def test_a_rescore_does_not_read_its_own_row(self, ch):
        """Re-detecting a stored cycle must not count it as its own history."""
        tx_ab, _ = _plant(ch, lambda o: [(o[0], _LEG_3)])
        first, _ = _score_and_store(ch, tx_ab)

        again = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert again["prior_cycles_in_window"] == 0
        assert again["recipient_entropy"] == first["recipient_entropy"]

    def test_the_window_is_the_chain_time_before_the_cycle(self, ch):
        """Only a cycle that started within recurrence_window_days before this
        one counts: not an older one, and not a later one a re-score would see."""
        ring = _ring()
        origins, addr_b, addr_c = ring
        window = graph._RECURRENCE_WINDOW_SLOTS
        # Past a full window from slot 0, so the older prior has a slot to sit at.
        origin_slot = _ORIGIN_SLOT + window
        for first_slot in (
            origin_slot - window + _WINDOW_EDGE_SLOTS,
            origin_slot - window - _WINDOW_EDGE_SLOTS,
            origin_slot + _WINDOW_EDGE_SLOTS,
        ):
            evidence = _prior(first_slot, [addr_b, addr_c], origins)
            _store_score(ch, uuid.uuid4().hex * 2, BAND_HIGH_THRESHOLD, evidence)
        tx_ab, _ = _plant(ch, lambda o: [(o[0], _LEG_3)], ring=ring, origin_slot=origin_slot)

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle["prior_cycles_in_window"] == 1, "only the one inside the window"
        assert cycle["recurrence_count"] == 1

    def test_only_this_origin_s_scored_or_suppressed_cycles_are_history(self, ch):
        """Of the stored rows, history is this origin's cycles on this network,
        scored or suppressed; recurrence is the High ones of this same ring."""
        ring = _ring()
        origins, addr_b, addr_c = ring
        inside = _ORIGIN_SLOT - _WINDOW_EDGE_SLOTS
        same_ring = [addr_b, addr_c]
        other_ring = [f"addr_test1qq_other_{uuid.uuid4().hex[:8]}"]
        stranger = [f"addr_test1qq_stranger_{uuid.uuid4().hex[:8]}"]
        rows = [
            # Counted: suppressed, below High, and a High of another ring.
            (_NO_FINDING, _prior(inside, same_ring, origins), LIVE_NETWORK),
            (_BELOW_HIGH, _prior(inside, same_ring, origins), LIVE_NETWORK),
            (BAND_HIGH_THRESHOLD, _prior(inside, other_ring, origins), LIVE_NETWORK),
            # Not counted: another network, another origin, no finding and no path.
            (BAND_HIGH_THRESHOLD, _prior(inside, same_ring, origins), f"{LIVE_NETWORK}_other"),
            (BAND_HIGH_THRESHOLD, _prior(inside, same_ring, stranger), LIVE_NETWORK),
            (_NO_FINDING, _prior(inside, [], origins), LIVE_NETWORK),
        ]
        for score, evidence, network in rows:
            _store_score(ch, uuid.uuid4().hex * 2, score, evidence, network=network)
        tx_ab, _ = _plant(ch, lambda o: [(o[0], _LEG_3)], ring=ring)

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle["prior_cycles_in_window"] == 3
        assert cycle["recurrence_count"] == 0, "the only High shares no intermediary"
        assert cycle["recycled_share"] == 1.0

    def test_an_earlier_cycle_from_another_address_of_the_wallet_is_history(self, ch):
        """The origin is every address the wallet spent from, not only the one
        that sorts first."""
        ring = _ring(origin_count=2)
        origins, addr_b, addr_c = ring
        evidence = _prior(_ORIGIN_SLOT - _WINDOW_EDGE_SLOTS, [addr_b, addr_c], [origins[1]])
        _store_score(ch, uuid.uuid4().hex * 2, BAND_HIGH_THRESHOLD, evidence)
        tx_ab, _ = _plant(ch, lambda o: [(o[0], _LEG_3)], ring=ring)

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle["prior_cycles_in_window"] == 1
        assert cycle["recurrence_count"] == 1

    def test_a_suppressed_pass_counts_for_the_next(self, ch):
        """A slow ring with non-round amounts is suppressed as structural-only on
        its own. The suppressed pass is still stored with its path, so the
        repeat reads it and is surfaced."""
        ring = _ring()
        legs = (_LEG_1 + _ODD_LOVELACE, _LEG_2 + _ODD_LOVELACE)
        repay = _LEG_3 + _ODD_LOVELACE
        first_tx, _ = _plant(
            ch,
            lambda o: [(o[0], repay)],
            ring=ring,
            leg_gap=_SLOW_LEG_SLOT_GAP,
            leg_amounts=legs,
        )
        _, first_result = _score_and_store(ch, first_tx)
        assert first_result.score == _NO_FINDING

        again_tx, _ = _plant(
            ch,
            lambda o: [(o[0], repay)],
            ring=ring,
            origin_slot=_ORIGIN_SLOT + _REPLAY_OFFSET_SLOTS,
            leg_gap=_SLOW_LEG_SLOT_GAP,
            leg_amounts=legs,
        )
        again = graph.detect_cycle(again_tx, LIVE_NETWORK)

        assert again["prior_cycles_in_window"] == 1
        assert CircularScorer().score({"cycle": again, "network": LIVE_NETWORK}).score >= 0

    def test_the_path_leaves_out_a_decoy_and_keeps_the_closer(self, ch):
        """The intermediaries are the addresses that carried the value back: an
        output to an address that sorts first and never pays back is not one,
        and the address that pays the origin back is."""
        ring = _ring()
        decoy = f"addr_test1qqa_decoy_{uuid.uuid4().hex[:8]}"
        tx_ab, _ = _plant(
            ch, lambda o: [(o[0], _LEG_3)], ring=ring, extra_origin_outputs=[(decoy, _DUST)]
        )

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle["intermediaries"] == [ring[1], ring[2]]
        assert cycle["hops"][1]["address"] == decoy, "the decoy is the step's representative"

    def test_a_four_leg_ring_records_every_intermediary(self, ch):
        run = uuid.uuid4().hex[:12]
        origin, b, c, d = (f"addr_test1qq{leg}_{run}" for leg in ("o", "b", "c", "d"))
        tx_ab, tx_bc, tx_cd, tx_da = (uuid.uuid4().hex * 2 for _ in range(4))
        ch.insert_transactions_batch(
            [
                _tx(tx_ab, _ORIGIN_SLOT, [origin], [(b, _LEG_1)]),
                _tx(tx_bc, _ORIGIN_SLOT + _LEG_SLOT_GAP, [b], [(c, _LEG_2)]),
                _tx(tx_cd, _ORIGIN_SLOT + 2 * _LEG_SLOT_GAP, [c], [(d, _LEG_3)]),
                _tx(tx_da, _ORIGIN_SLOT + 3 * _LEG_SLOT_GAP, [d], [(origin, _LEG_3)]),
            ]
        )

        cycle = graph.detect_cycle(tx_ab, LIVE_NETWORK)

        assert cycle["cycle_length"] == 4
        assert cycle["intermediaries"] == [b, c, d]
