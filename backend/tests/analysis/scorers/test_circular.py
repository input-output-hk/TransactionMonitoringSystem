"""Unit tests for the Circular Transfers scorer (Class 7)."""

from unittest.mock import patch

import pytest

from app.analysis import graph
from app.analysis.features import LOVELACE_PER_ADA
from app.analysis.normalise import (
    BAND_HIGH_THRESHOLD,
    BAND_MODERATE_MAX,
    BAND_MODERATE_THRESHOLD,
)
from app.analysis.scorers import circular as circular_mod
from app.analysis.scorers.circular import CircularScorer


@pytest.fixture
def scorer():
    return CircularScorer()


def _features(cycle=None):
    return {
        "tx_hash": "ci01",
        "network": "preprod",
        "raw_data": {},
        "cycle": cycle,
    }


class TestGate:
    def test_no_cycle_data(self, scorer):
        assert scorer.gate(_features()) is False

    def test_cycle_too_short(self, scorer):
        assert scorer.gate(_features(cycle={"cycle_length": 1, "net_loss_ratio": 0.01})) is False

    def test_two_hop_roundtrip_rejected(self, scorer):
        # A 2-hop "cycle" (A -> script -> A) is a deposit/withdraw round-trip,
        # not circular layering; min_length is 3, so the gate rejects it. This
        # is the dominant former false positive.
        assert scorer.gate(_features(cycle={"cycle_length": 2, "net_loss_ratio": 0.01})) is False

    def test_cycle_too_long(self, scorer):
        assert scorer.gate(_features(cycle={"cycle_length": 7, "net_loss_ratio": 0.01})) is False

    def test_high_net_loss_rejected(self, scorer):
        """Loss much greater than fee tolerance should be rejected."""
        assert scorer.gate(_features(cycle={"cycle_length": 3, "net_loss_ratio": 0.50})) is False

    def test_valid_cycle_passes(self, scorer):
        assert scorer.gate(_features(cycle={"cycle_length": 3, "net_loss_ratio": 0.04})) is True


class TestScore:
    @pytest.mark.attack_must_fire
    def test_high_similarity_scores_well(self, scorer):
        """A recurring 3-hop cycle with near-identical amounts must stay High.

        Measured 82.71, which clears Critical by under three points; the floor
        sits at High for the same reason as the sandwich case.
        """
        cycle = {
            "cycle_length": 3,
            "addresses": ["a", "b", "c"],
            "amount_similarity": 0.95,
            "net_loss_ratio": 0.03,
            "recurrence_count": 4,
            "recipient_entropy": 0.40,
            "round_amount_flag": True,
            "temporal_concentration": 0.70,
            "mean_inter_hop_delta_slots": 3.0,
            "origin_cluster": "cluster01",
        }
        result = scorer.score(_features(cycle=cycle))
        assert result.score >= BAND_HIGH_THRESHOLD
        assert result.sub_scores["amount_similarity"] > 0.5

    def test_low_entropy_boosts_score(self, scorer):
        """Low recipient entropy (same addresses) should increase entropy_inv sub-score."""
        high_entropy = {
            "cycle_length": 4,
            "amount_similarity": 0.80,
            "net_loss_ratio": 0.04,
            "recurrence_count": 1,
            "recipient_entropy": 0.90,  # high = normal
            "round_amount_flag": False,
            "temporal_concentration": 0.2,
            "mean_inter_hop_delta_slots": 20,
            "origin_cluster": "c",
        }
        low_entropy = dict(high_entropy, recipient_entropy=0.15)  # low = suspicious
        r_high = scorer.score(_features(cycle=high_entropy))
        r_low = scorer.score(_features(cycle=low_entropy))
        assert (
            r_low.sub_scores["recipient_entropy_inv"] > r_high.sub_scores["recipient_entropy_inv"]
        )

    def test_no_cycle_returns_zero(self, scorer):
        result = scorer.score(_features())
        assert result.score == 0.0

    def test_sub_scores_keys(self, scorer):
        cycle = {
            "cycle_length": 2,
            "amount_similarity": 0.80,
            "net_loss_ratio": 0.02,
            "recurrence_count": 0,
            "recipient_entropy": 0.70,
            "round_amount_flag": False,
            "temporal_concentration": 0.0,
            "mean_inter_hop_delta_slots": 50,
            "origin_cluster": "x",
        }
        result = scorer.score(_features(cycle=cycle))
        for key in (
            "amount_similarity",
            "cycle_recurrence",
            "recipient_entropy_inv",
            "auxiliary",
            "speed",
        ):
            assert key in result.sub_scores

    def test_structural_only_suppressed(self, scorer):
        """amount_similarity + cycle_recurrence alone, with no corroborating
        evidence (entropy/auxiliary/speed all near-zero), is a plain Plutus
        user->script->user interaction. It must be suppressed entirely (score
        -1, no finding), not surfaced at a capped Moderate."""
        structural_only = {
            "cycle_length": 2,
            "amount_similarity": 1.0,  # ceiling
            "net_loss_ratio": 0.01,  # fee-only
            "recurrence_count": 100,  # ceiling
            "recipient_entropy": 1.0,  # max (entropy_inv = 0)
            "round_amount_flag": False,
            "temporal_concentration": 0.0,
            "mean_inter_hop_delta_slots": 1_000_000,
            "origin_cluster": "c",
        }
        result = scorer.score(_features(cycle=structural_only))
        assert result.score == -1.0, (
            f"structural-only cycle should be suppressed; got {result.score}"
        )

    def test_a_suppressed_cycle_still_stores_its_path(self, scorer):
        """No finding, but the origin's next cycle must be able to read this one
        back: the circular_* columns are derived from this evidence."""
        cycle = {
            "cycle_length": 3,
            "amount_similarity": 1.0,
            "net_loss_ratio": 0.01,
            "recurrence_count": 0,
            "recipient_entropy": 1.0,
            "round_amount_flag": False,
            "temporal_concentration": 0.0,
            "mean_inter_hop_delta_slots": 1_000_000,
            "origin_cluster": "o",
            "hops": [{"address": "o", "amount_lovelace": 1, "slot": 7}],
            "intermediaries": ["b", "c"],
            "origin_keys": ["o"],
        }
        result = scorer.score(_features(cycle=cycle))
        assert result.score == -1.0
        assert result.evidence["intermediaries"] == ["b", "c"]
        assert result.evidence["origin_keys"] == ["o"]
        assert result.evidence["first_slot"] == 7

    def test_structural_plus_signal_uncapped(self, scorer):
        """When corroborating signals are present (low entropy, round amounts,
        fast hops), the structural cap should NOT fire and the score should
        rise into High."""
        corroborated = {
            "cycle_length": 2,
            "amount_similarity": 1.0,
            "net_loss_ratio": 0.01,
            "recurrence_count": 5,
            "recipient_entropy": 0.10,  # very low => entropy_inv high
            "round_amount_flag": True,
            "temporal_concentration": 0.90,  # concentrated in time
            "mean_inter_hop_delta_slots": 2,
            "origin_cluster": "c",
        }
        result = scorer.score(_features(cycle=corroborated))
        # Pinned to the band constant rather than a literal just under it, so
        # the bar moves with the bands if they are ever retuned.
        assert result.score >= BAND_HIGH_THRESHOLD, (
            f"corroborated cycle should escape the Moderate cap; got {result.score}"
        )


class TestRecurringLayeringEscape:
    """Recall-first: a cycle that closes to origin with strongly preserved
    amounts AND repeated recurrence is deliberate layering even when it uses
    many FRESH intermediary addresses (high recipient entropy -> s_entropy ~ 0,
    which marks it structural-only). It must be surfaced at a capped Moderate
    rather than suppressed to no-finding."""

    def _fresh_address_cycle(self, recurrence_count):
        # High amount similarity + given recurrence, but fresh addresses (max
        # entropy), no round amounts, no temporal concentration, slow hops ->
        # entropy + auxiliary + speed all ~0 (structural-only).
        return {
            "cycle_length": 3,
            "addresses": ["fresh_a", "fresh_b", "fresh_c"],
            "amount_similarity": 0.99,
            "net_loss_ratio": 0.03,
            "recurrence_count": recurrence_count,
            "recipient_entropy": 1.0,  # all-distinct hops -> s_entropy ~ 0
            "round_amount_flag": False,
            "temporal_concentration": 0.0,
            "mean_inter_hop_delta_slots": 100000.0,  # very slow -> s_speed ~ 0
            "origin_cluster": "cluster_layer",
        }

    def test_recurring_fresh_address_cycle_capped_not_suppressed(self, scorer):
        result = scorer.score(_features(cycle=self._fresh_address_cycle(recurrence_count=25)))
        # A finding is surfaced (not the -1 no_finding sentinel)...
        assert result.score >= 0
        # ...and it is capped at Moderate, never escalated to High/Critical.
        from app.analysis.scorers.circular import _MODERATE_CAP

        assert result.score <= _MODERATE_CAP

    def test_benign_structural_cycle_still_suppressed(self, scorer):
        # Same shape but NO recurrence: a plain structural round-trip stays
        # suppressed (no false-positive regression).
        result = scorer.score(_features(cycle=self._fresh_address_cycle(recurrence_count=0)))
        assert result.score == -1.0


# End-to-end from the hop query: detect_cycle builds the cycle from ClickHouse
# rows and CircularScorer judges it, so these cover the path the hand-fed cycles
# above cannot, where final_amount (and so net_loss_ratio) is measured.
#
# A 3-leg cycle out of a wallet that spent from two of its addresses, through
# two intermediaries and back. ~0.2% is lost end to end, far inside the gate's
# fee tolerance, and the amount sent is a whole number of ADA.
_ORIGIN_TX = "e2e0" * 16
_LEG_2_TX = "e2e2" * 16
_CLOSING_TX = "e2ec" * 16
_ORIGIN_A = "addr1q_e2e_origin_a"  # sorts ahead of _ORIGIN_B
_ORIGIN_B = "addr1q_e2e_origin_b"
_HOP_1 = "addr1q_e2e_hop_1"
_HOP_2 = "addr1q_e2e_hop_2"
# A 1 ADA output of the origin transaction to an address that sorts ahead of
# the ring's first hop: routine as change or dust, and free to add.
_DECOY = "addr1q_e2e_decoy"
_RING = [_HOP_1, _HOP_2]
_ORIGIN_SLOT = 1_000_000
_LEG_GAP_SLOTS = 3
# Legs one block apart or more: at this gap the speed and timing axes read 0.
_SLOW_LEG_GAP_SLOTS = 25
_SENT = 10_000 * LOVELACE_PER_ADA
_LEG_2 = 9_990 * LOVELACE_PER_ADA
_REPAID = 9_979 * LOVELACE_PER_ADA
# Added to every leg to make the amounts non-round (not a whole ADA).
_ODD_LOVELACE = 170_001
# A min-UTxO-sized extra output back to the wallet: routine on Cardano as token
# dust or change, and free for an attacker to add.
_DUST = 1 * LOVELACE_PER_ADA
# Change the closing leg sends back to the wallet beside the repayment, ~20% of
# the cycled amount: routine when one wallet controls every leg.
_CHANGE = 2_000 * LOVELACE_PER_ADA
# Value the closing leg re-locks at a script the origin also spent from, far
# larger than the cycle itself.
_RELOCK = 500_000 * LOVELACE_PER_ADA
# Prior High findings of this same ring, as in the hand-fed must-fire case
# above, so both describe the same recurring pattern.
_PRIOR_CYCLES = 4
_PRIOR_HIGHS = [(BAND_HIGH_THRESHOLD, list(_RING))] * _PRIOR_CYCLES
# Earlier cycles of the same origin through addresses this ring never uses.
_OTHER_RINGS = [
    (BAND_HIGH_THRESHOLD, [f"addr1q_e2e_other_{i}", f"addr1q_e2e_other_{i}b"])
    for i in range(_PRIOR_CYCLES)
]


class _PlantedCycleClient:
    """Answers detect_cycle's queries as ClickHouse would for one planted cycle.

    ``closing_outputs`` are every output of the closing transaction as
    (address, amount). The hop query sees them the way its SQL returns them,
    DISTINCT and sorted by address then amount DESC; the closing-leg read sees
    them without DISTINCT, filtered to the transaction and the origin set it is
    asked about. ``fail_closing_read`` makes that read raise. ``prior_cycles``
    are the history read's rows, (circular, circular_intermediaries) per
    earlier cycle of the origin.
    """

    def __init__(
        self,
        closing_outputs: list[tuple[str, int]],
        prior_cycles: list[tuple] = _PRIOR_HIGHS,
        fail_history_read: bool = False,
        fail_closing_read: bool = False,
        gap: int = _LEG_GAP_SLOTS,
        sent: int = _SENT,
        leg_2: int = _LEG_2,
        decoy: bool = False,
        two_hop: bool = False,
    ):
        close_slot = _ORIGIN_SLOT + (gap if two_hop else 2 * gap)
        closing_rows = sorted(
            {(_CLOSING_TX, addr, amount, close_slot) for addr, amount in closing_outputs},
            key=lambda r: (r[1], -r[2]),
        )
        leg_2_row = (_LEG_2_TX, _HOP_2, leg_2, _ORIGIN_SLOT + gap)
        self._hops = [closing_rows] if two_hop else [[leg_2_row], closing_rows]
        self._closing_outputs = closing_outputs
        self._fail_closing_read = fail_closing_read
        self._prior_cycles = prior_cycles
        self._fail_history_read = fail_history_read
        self._origin_outputs = [(_HOP_1, sent, _ORIGIN_SLOT)]
        if decoy:
            self._origin_outputs.append((_DECOY, _DUST, _ORIGIN_SLOT))
        # Who spent into each transaction on the ring, for the path read.
        closer = _HOP_1 if two_hop else _HOP_2
        self._spends = {_LEG_2_TX: [_HOP_1], _CLOSING_TX: [closer]}

    def execute(self, sql, params=None, **_):
        if "WITH cand AS" in sql:
            return self._hops.pop(0) if self._hops else []
        if "SELECT address, amount" in sql:
            if self._fail_closing_read:
                raise ConnectionError("planted closing-leg read failure")
            if params["tx_hash"] != _CLOSING_TX:
                return []
            return [(a, amt) for a, amt in self._closing_outputs if a in params["origin"]]
        if "tx_class_scores" in sql:
            if self._fail_history_read:
                raise ConnectionError("ClickHouse went away")
            return self._prior_cycles
        if "SELECT DISTINCT address" in sql and "txs" in params:
            return [(a,) for tx in params["txs"] for a in self._spends.get(tx, ())]
        if "SELECT DISTINCT address" in sql:
            return [(_ORIGIN_A,), (_ORIGIN_B,)]
        if "SELECT o.address, o.amount, t.slot" in sql:
            return self._origin_outputs
        raise AssertionError(f"unexpected query: {sql.strip()[:80]}")


def _detect(closing_outputs: list[tuple[str, int]], **client_opts) -> dict | None:
    with patch("app.analysis.graph.clickhouse") as mock_ch:
        mock_ch._get_client.return_value = _PlantedCycleClient(closing_outputs, **client_opts)
        return graph.detect_cycle(_ORIGIN_TX, "preprod")


class TestClosingLegFromTheHopQuery:
    @pytest.mark.attack_must_fire
    def test_dust_to_a_second_origin_address_does_not_mask_the_repayment(self, scorer):
        """Dust to the origin address that sorts first must not stand in for
        the repayment to the other one.

        The hop query sorts by address, so its first origin-paying row is the
        dust. Taking that row's amount as final_amount put net_loss_ratio at
        ~0.9999 and the gate discarded the cycle outright: no finding at all.
        Measured 87.11 through the full path, so the floor sits at High.
        """
        cycle = _detect([(_ORIGIN_A, _DUST), (_ORIGIN_B, _REPAID)])
        assert cycle is not None and cycle["cycle_length"] == 3
        features = _features(cycle=cycle)
        assert scorer.gate(features), f"gate rejected: net_loss_ratio={cycle['net_loss_ratio']}"
        assert scorer.score(features).score >= BAND_HIGH_THRESHOLD

    @pytest.mark.attack_must_fire
    def test_repayment_split_into_equal_outputs_counts_in_full(self, scorer):
        """A repayment split into equal outputs must count in full.

        The hop query's DISTINCT collapses equal outputs to one address into a
        single row, so half the repayment looked lost and the gate discarded
        the cycle. Splitting an output is free, so this is the easier evasion.
        Measured 87.11 through the full path, so the floor sits at High.
        """
        half = _REPAID // 2
        cycle = _detect([(_ORIGIN_A, half), (_ORIGIN_A, half)])
        assert cycle is not None and cycle["cycle_length"] == 3
        features = _features(cycle=cycle)
        assert scorer.gate(features), f"gate rejected: net_loss_ratio={cycle['net_loss_ratio']}"
        assert scorer.score(features).score >= BAND_HIGH_THRESHOLD

    @pytest.mark.attack_must_fire
    @pytest.mark.parametrize(
        "closing_outputs",
        [
            [(_ORIGIN_A, _REPAID), (_ORIGIN_B, _CHANGE)],
            [(_ORIGIN_A, _REPAID), (_ORIGIN_A, _CHANGE)],
            [(_ORIGIN_A, _REPAID), (_ORIGIN_B, _RELOCK)],
            [(_ORIGIN_A, _REPAID // 2), (_ORIGIN_A, _REPAID // 2), (_ORIGIN_B, _CHANGE)],
            [(_ORIGIN_A, _REPAID // 2), (_ORIGIN_A, _REPAID // 2), (_ORIGIN_A, _CHANGE)],
            [
                (_ORIGIN_A, _REPAID // 3),
                (_ORIGIN_A, _REPAID // 3),
                (_ORIGIN_A, _REPAID - 2 * (_REPAID // 3)),
                (_ORIGIN_A, 2 * _CHANGE),
                (_ORIGIN_B, 2 * _CHANGE),
            ],
            [(_ORIGIN_A, _REPAID // 2), (_ORIGIN_B, _REPAID // 2), (_ORIGIN_A, _RELOCK)],
        ],
        ids=[
            "change-to-second-origin-address",
            "change-to-same-address",
            "script-re-lock",
            "split-repayment-and-change",
            "split-repayment-and-change-to-the-same-address",
            "repayment-in-thirds-beside-two-changes",
            "halves-to-two-addresses-and-a-re-lock",
        ],
    )
    def test_extra_value_back_to_the_wallet_does_not_demote_the_cycle(
        self, scorer, closing_outputs
    ):
        """Value the closing leg pays the wallet beyond the repayment must not
        count as a mismatch between hops.

        The total returned is right for the loss gate, but as the closing hop's
        amount in amount_similarity it turned change, or a re-lock at a script
        the origin spent from, into an uneven cycle: against four earlier Highs
        of the same ring, #108's reading scored the change 78.5 and the re-lock
        37.11 (Moderate). The similarity now reads the part of the payment that
        fits the other hops best, whichever outputs it is split across, so each
        shape measures as the plain cycle does, 87.11.
        """
        plain = _detect([(_ORIGIN_A, _REPAID)])
        cycle = _detect(closing_outputs)
        assert cycle is not None and cycle["cycle_length"] == 3
        assert cycle["amount_similarity"] == plain["amount_similarity"]
        features = _features(cycle=cycle)
        assert scorer.gate(features), f"gate rejected: net_loss_ratio={cycle['net_loss_ratio']}"
        assert scorer.score(features).score >= BAND_HIGH_THRESHOLD

    def test_a_failed_closing_leg_read_keeps_the_cycle_and_asks_for_a_retry(self, scorer):
        """A failed read of the closing leg must not cost the finding.

        The cycle is measured with the matched row, which is exact when the
        closing leg pays the origin one output, and flagged so the engine
        retries the transaction for the full reading; if the read keeps
        failing, the engine writes this score with a marker.
        """
        plain = _detect([(_ORIGIN_A, _REPAID)])
        cycle = _detect([(_ORIGIN_A, _REPAID)], fail_closing_read=True)
        assert cycle["closing_leg_unavailable"] is True
        assert plain["closing_leg_unavailable"] is False
        features = _features(cycle=cycle)
        assert scorer.gate(features), f"gate rejected: net_loss_ratio={cycle['net_loss_ratio']}"
        assert scorer.score(features).score == scorer.score(_features(cycle=plain)).score

    def test_a_two_hop_closure_reads_no_closing_leg(self):
        """The gate never scores a two-hop closure, so it must not cost a read or
        a retry when the read would fail."""
        cycle = _detect([(_ORIGIN_A, _REPAID)], fail_closing_read=True, two_hop=True)
        assert cycle is not None and cycle["cycle_length"] == 2
        assert cycle["closing_leg_unavailable"] is False


class TestRecyclingOverTheWindow:
    """The recipient-entropy axis reads how much of this cycle's path the
    origin's earlier cycles in the window already went through, and recurrence
    counts the earlier Highs of the same ring. One cycle cannot show either on
    its own: the forward search never revisits an address.
    """

    def _cycle(self, prior_cycles, gap=_LEG_GAP_SLOTS, odd=0, closing=None, **client_opts):
        return _detect(
            closing or [(_ORIGIN_A, _REPAID + odd)],
            prior_cycles=prior_cycles,
            gap=gap,
            sent=_SENT + odd,
            leg_2=_LEG_2 + odd,
            **client_opts,
        )

    def _score(self, scorer, prior_cycles, **kwargs):
        cycle = self._cycle(prior_cycles, **kwargs)
        assert cycle is not None and cycle["cycle_length"] == 3
        features = _features(cycle=cycle)
        assert scorer.gate(features), f"gate rejected: net_loss_ratio={cycle['net_loss_ratio']}"
        return cycle, scorer.score(features)

    def _second_pass(self, scorer, **kwargs):
        """Score the ring once against no history, store it the way production
        does, and score it again. The first pass is stored at no more than the
        top of Moderate, so the second pass is lifted by the recycled path alone
        whatever a future change does to one-off rings."""
        first, first_result = self._score(scorer, prior_cycles=[], **kwargs)
        stored = (
            min(first_result.score, BAND_MODERATE_MAX),
            first_result.evidence["intermediaries"],
        )
        again, result = self._score(scorer, prior_cycles=[stored], **kwargs)
        return first_result, again, result

    @pytest.mark.attack_must_fire
    def test_a_ring_repeated_through_the_same_intermediaries_reaches_high(self, scorer):
        """The second pass of a ring through the same intermediaries must page.

        Measured 43.11 then 63.11, so the floor sits at High.
        """
        first, again, result = self._second_pass(scorer)
        assert first.evidence["intermediaries"] == _RING
        assert again["recycled_share"] == 1.0
        assert again["recurrence_count"] == 0
        assert "low_recipient_diversity" in result.reasons
        assert result.score >= BAND_HIGH_THRESHOLD

    @pytest.mark.attack_must_fire
    def test_a_ring_chained_within_one_block_repeated_reaches_high(self, scorer):
        """The TMS Forge shape: every leg in one block, amounts not a whole ADA.

        Hops sharing a slot once read as unmeasurable and scored 0 on speed, so
        this ring stopped at 55 however often it repeated. Measured
        45.0 then 65.0, so the floor sits at High.
        """
        _, again, result = self._second_pass(scorer, gap=0, odd=_ODD_LOVELACE)
        assert again["recycled_share"] == 1.0
        assert result.score >= BAND_HIGH_THRESHOLD

    @pytest.mark.attack_must_fire
    @pytest.mark.parametrize(
        "closing",
        [
            [(_ORIGIN_A, _REPAID), (_ORIGIN_B, _CHANGE)],
            [(_ORIGIN_A, _REPAID), (_ORIGIN_B, _RELOCK)],
            [(_ORIGIN_A, _REPAID // 2), (_ORIGIN_A, _REPAID // 2), (_ORIGIN_A, _CHANGE)],
        ],
        ids=[
            "change-to-second-origin-address",
            "script-re-lock",
            "split-repayment-and-change-to-the-same-address",
        ],
    )
    def test_a_repeated_ring_paying_the_wallet_extra_on_close_reaches_high(self, scorer, closing):
        """Value the closing leg pays the wallet beyond the repayment must not
        keep a repeated ring below High.

        The recycling credit and the amount axis both read amount_similarity,
        and with the closing hop read as the total paid to the origin set the
        extra counted as a mismatch: change, split repayment or not, stopped
        the second pass at 54.5, and the re-lock left the ring without the
        credit, at 13.11 on both passes. Measured 43.11 then 63.11 for each,
        so the floor sits at High.
        """
        _, again, result = self._second_pass(scorer, closing=closing)
        assert again["recycled_share"] == 1.0
        assert again["recurrence_count"] == 0
        assert result.score >= BAND_HIGH_THRESHOLD

    @pytest.mark.attack_must_fire
    def test_a_slow_ring_suppressed_once_surfaces_when_it_repeats(self, scorer):
        """Legs a block or more apart and a non-round amount leave only the two
        structural axes, so each pass alone is suppressed as structural-only.
        The suppressed pass still stores its path, and the repeat is surfaced.
        Measured -1 then 50.0 (Moderate).
        """
        first, again, result = self._second_pass(scorer, gap=_SLOW_LEG_GAP_SLOTS, odd=_ODD_LOVELACE)
        assert first.score == -1.0
        assert again["prior_cycles_in_window"] == 1
        assert result.score >= BAND_MODERATE_THRESHOLD

    def test_earlier_cycles_through_other_addresses_add_nothing(self, scorer):
        """The precision half: an origin that cycles often, through different
        addresses each time, must read exactly as a first-time ring."""
        _, alone = self._score(scorer, prior_cycles=[])
        cycle, result = self._score(scorer, prior_cycles=_OTHER_RINGS)
        assert cycle["prior_cycles_in_window"] == _PRIOR_CYCLES
        assert cycle["recycled_share"] == 0.0
        assert cycle["recurrence_count"] == 0
        assert result.score == alone.score

    def test_earlier_cycles_through_other_addresses_do_not_dilute_a_repeat(self, scorer):
        """Fresh rings from the same origin between two passes of a ring must
        not bring the repeat back down."""
        _, repeated = self._score(scorer, prior_cycles=[(BAND_MODERATE_MAX, list(_RING))])
        diluted_history = [(BAND_MODERATE_MAX, list(_RING)), *_OTHER_RINGS]
        _, diluted = self._score(scorer, prior_cycles=diluted_history)
        assert (
            diluted.sub_scores["recipient_entropy_inv"]
            == repeated.sub_scores["recipient_entropy_inv"]
        )

    def test_only_earlier_highs_of_the_same_ring_count_as_recurrence(self, scorer):
        """Recurrence counts earlier cycles that reached High, as Polimi
        Section 4.7.3 requires, and only those of this ring: an origin's High
        through other addresses is not this cycle repeating."""
        prior = [
            (BAND_HIGH_THRESHOLD, list(_RING)),
            (BAND_MODERATE_MAX, list(_RING)),
            (BAND_HIGH_THRESHOLD, ["addr1q_e2e_other_x", "addr1q_e2e_other_y"]),
        ]
        cycle, _ = self._score(scorer, prior_cycles=prior)
        assert cycle["recurrence_count"] == 1
        assert cycle["prior_cycles_in_window"] == len(prior)

    def test_a_decoy_output_does_not_hide_the_recycled_path(self, scorer):
        """The intermediaries are the addresses that carried value back, not the
        first address of each step in sort order."""
        cycle, result = self._score(
            scorer, prior_cycles=[(BAND_MODERATE_MAX, list(_RING))], decoy=True
        )
        assert cycle["intermediaries"] == _RING
        assert cycle["recycled_share"] == 1.0
        assert result.score >= BAND_HIGH_THRESHOLD

    def test_a_failed_history_read_keeps_the_cycle_and_asks_for_a_retry(self, scorer):
        """A failed read must not cost the finding: the cycle is scored as if it
        had no history, and flagged so the engine retries the transaction."""
        _, alone = self._score(scorer, prior_cycles=[])
        cycle, result = self._score(scorer, prior_cycles=_PRIOR_HIGHS, fail_history_read=True)
        assert cycle["history_unavailable"] is True
        assert cycle["prior_cycles_in_window"] == 0
        assert result.score == alone.score

    def test_a_two_hop_round_trip_reads_no_history(self):
        """The gate never scores a two-hop closure, so it must not cost a read or
        a retry when the read would fail."""
        cycle = _detect(
            [(_ORIGIN_A, _REPAID)],
            fail_history_read=True,
            two_hop=True,
        )
        assert cycle is not None and cycle["cycle_length"] == 2
        assert cycle["history_unavailable"] is False


class TestRecycledAddressesWithoutValuePreservation:
    def test_stays_below_high_even_with_every_other_axis_maxed(self, scorer):
        """The shape of nearly every mainnet cycle: bots and batchers that
        cycle through the same few addresses but do not pass the same value
        along. Recycled intermediaries alone must not page them: without
        amount preservation or an earlier High of the same ring the class tops
        out at the entropy, auxiliary and speed weights together."""
        cycle = {
            "cycle_length": 3,
            "amount_similarity": 0.0,
            "net_loss_ratio": 0.01,
            "recurrence_count": 0,
            "recipient_entropy": 0.0,
            "round_amount_flag": True,
            "temporal_concentration": 1.0,
            "mean_inter_hop_delta_slots": 1.0,
            "origin_cluster": "bot",
        }
        result = scorer.score(_features(cycle=cycle))
        assert result.sub_scores["recipient_entropy_inv"] == 1.0
        ceiling = 100 * sum(
            float(circular_mod._W[axis]) for axis in ("entropy", "auxiliary", "speed")
        )
        assert result.score == pytest.approx(ceiling)
        assert result.score < BAND_HIGH_THRESHOLD
