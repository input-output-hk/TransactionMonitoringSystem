"""Unit tests for the Circular Transfers scorer (Class 7)."""

from unittest.mock import patch

import pytest

from app.analysis import graph
from app.analysis.features import LOVELACE_PER_ADA
from app.analysis.normalise import BAND_HIGH_THRESHOLD
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
_CLOSING_TX = "e2ec" * 16
_ORIGIN_A = "addr1q_e2e_origin_a"  # sorts ahead of _ORIGIN_B
_ORIGIN_B = "addr1q_e2e_origin_b"
_HOP_1 = "addr1q_e2e_hop_1"
_HOP_2 = "addr1q_e2e_hop_2"
_ORIGIN_SLOT = 1_000_000
_LEG_GAP_SLOTS = 3
_SENT = 10_000 * LOVELACE_PER_ADA
_LEG_2 = 9_990 * LOVELACE_PER_ADA
_REPAID = 9_979 * LOVELACE_PER_ADA
# A min-UTxO-sized extra output back to the wallet: routine on Cardano as token
# dust or change, and free for an attacker to add.
_DUST = 1 * LOVELACE_PER_ADA
# Prior circular findings from this origin, as in the hand-fed must-fire case
# above, so both describe the same recurring pattern.
_PRIOR_CYCLES = 4


class _PlantedCycleClient:
    """Answers detect_cycle's queries as ClickHouse would for one planted cycle.

    ``closing_rows`` are the hop query's rows for the closing transaction, in
    that query's sort order and with its DISTINCT applied; ``closing_total`` is
    what the transaction really paid the origin set, which only a lookup without
    DISTINCT sees.
    """

    def __init__(self, closing_rows: list[tuple], closing_total: int):
        leg_2_slot = _ORIGIN_SLOT + _LEG_GAP_SLOTS
        self._hops = [[("e2e2" * 16, _HOP_2, _LEG_2, leg_2_slot)], closing_rows]
        self._closing_total = closing_total

    def execute(self, sql, params=None, **_):
        if "WITH cand AS" in sql:
            return self._hops.pop(0) if self._hops else []
        if "sum(amount)" in sql:
            return [(self._closing_total,)]
        if "tx_class_scores" in sql:
            return [(_PRIOR_CYCLES,)]
        if "SELECT DISTINCT address" in sql:
            return [(_ORIGIN_A,), (_ORIGIN_B,)]
        if "SELECT o.address, o.amount, t.slot" in sql:
            return [(_HOP_1, _SENT, _ORIGIN_SLOT)]
        raise AssertionError(f"unexpected query: {sql.strip()[:80]}")


def _detect(closing_rows: list[tuple], closing_total: int) -> dict | None:
    with patch("app.analysis.graph.clickhouse") as mock_ch:
        mock_ch._get_client.return_value = _PlantedCycleClient(closing_rows, closing_total)
        return graph.detect_cycle(_ORIGIN_TX, "preprod")


class TestClosingLegFromTheHopQuery:
    _CLOSE_SLOT = _ORIGIN_SLOT + 2 * _LEG_GAP_SLOTS

    @pytest.mark.attack_must_fire
    def test_dust_to_a_second_origin_address_does_not_mask_the_repayment(self, scorer):
        """Dust to the origin address that sorts first must not stand in for
        the repayment to the other one.

        The hop query sorts by address, so its first origin-paying row is the
        dust. Taking that row's amount as final_amount put net_loss_ratio at
        ~0.9999 and the gate discarded the cycle outright: no finding at all.
        Measured 67.11 through the full path, so the floor sits at High.
        """
        cycle = _detect(
            closing_rows=[
                (_CLOSING_TX, _ORIGIN_A, _DUST, self._CLOSE_SLOT),
                (_CLOSING_TX, _ORIGIN_B, _REPAID, self._CLOSE_SLOT),
            ],
            closing_total=_DUST + _REPAID,
        )
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
        Measured 67.11 through the full path, so the floor sits at High.
        """
        half = _REPAID // 2
        cycle = _detect(
            closing_rows=[(_CLOSING_TX, _ORIGIN_A, half, self._CLOSE_SLOT)],
            closing_total=2 * half,
        )
        assert cycle is not None and cycle["cycle_length"] == 3
        features = _features(cycle=cycle)
        assert scorer.gate(features), f"gate rejected: net_loss_ratio={cycle['net_loss_ratio']}"
        assert scorer.score(features).score >= BAND_HIGH_THRESHOLD
