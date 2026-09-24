"""Unit tests for the transfer graph cycle detection module."""

import itertools
import math
import random
from unittest.mock import MagicMock, patch

from app.analysis import graph
from app.analysis.graph import _build_cycle_result, detect_cycle
from app.analysis.normalise import BAND_HIGH_THRESHOLD
from app.utils.bech32 import _CHARSET, convertbits

# CIP-19 header bytes (address type in the high nibble, network id 1 = mainnet).
_KEY_BASE_HEADER = 0x01  # key payment, key stake: "addr1q..."
_SCRIPT_BASE_HEADER = 0x11  # script payment, key stake: "addr1z..."
_KEY_ENTERPRISE_HEADER = 0x61  # key payment, no stake: "addr1v..."
# A score just under High: an earlier cycle at this score is pooled for the
# recycled share but never counted as recurrence.
_BELOW_HIGH = BAND_HIGH_THRESHOLD - 1


def _address(header: int, payment: str, stake: str = "") -> str:
    """A mainnet Shelley address with the given credentials.

    The six trailing characters stand in for the bech32 checksum, which
    payment_credential_or_raw deliberately strips without checking.
    """
    raw = bytes([header]) + bytes.fromhex(payment + stake)
    data = convertbits(list(raw), 8, 5, pad=True)
    return "addr1" + "".join(_CHARSET[d] for d in data) + "qqqqqq"


_MULE_KEY = "ab" * 28
_SCRIPT_HASH = "cd" * 28
_STAKE_1 = "01" * 28
_STAKE_2 = "02" * 28


class TestBuildCycleResult:
    def test_basic_cycle_metrics(self):
        result = _build_cycle_result(
            cycle_length=3,
            addresses=["addr_a", "addr_b", "addr_c", "addr_a"],
            origin_amount=10_000_000,
            final_amount=9_500_000,
            hops=[
                {"address": "addr_a", "amount_lovelace": 10_000_000, "slot": 100},
                {"address": "addr_b", "amount_lovelace": 9_800_000, "slot": 105},
                {"address": "addr_a", "amount_lovelace": 9_500_000, "slot": 110},
            ],
            origin_addresses={"addr_a"},
            intermediaries=["addr_b", "addr_c"],
            prior_cycles=[],
        )
        assert result["cycle_length"] == 3
        assert 0.9 < result["amount_similarity"] <= 1.0
        assert 0 < result["net_loss_ratio"] < 0.1
        assert result["origin_cluster"] == "addr_a"
        # The per-cycle reading this function computes: 1.5 bits over the three
        # distinct addresses, so a changed normalisation cannot pass unseen.
        assert result["recipient_entropy"] == round(1.5 / math.log2(3), 4)

    def test_round_amount_flag(self):
        result = _build_cycle_result(
            cycle_length=2,
            addresses=["a", "b", "a"],
            origin_amount=5_000_000,  # 5 ADA, round
            final_amount=4_800_000,
            hops=[
                {"address": "a", "amount_lovelace": 5_000_000, "slot": 10},
                {"address": "a", "amount_lovelace": 4_800_000, "slot": 15},
            ],
            origin_addresses={"a"},
            intermediaries=[],
            prior_cycles=[],
        )
        assert result["round_amount_flag"] is True

    def test_non_round_amount(self):
        result = _build_cycle_result(
            cycle_length=2,
            addresses=["a", "b", "a"],
            origin_amount=5_123_456,
            final_amount=5_000_000,
            hops=[
                {"address": "a", "amount_lovelace": 5_123_456, "slot": 10},
                {"address": "a", "amount_lovelace": 5_000_000, "slot": 15},
            ],
            origin_addresses={"a"},
            intermediaries=[],
            prior_cycles=[],
        )
        assert result["round_amount_flag"] is False

    def test_zero_origin_amount(self):
        result = _build_cycle_result(
            cycle_length=2,
            addresses=["a", "b"],
            origin_amount=0,
            final_amount=0,
            hops=[
                {"address": "a", "amount_lovelace": 0, "slot": 10},
                {"address": "b", "amount_lovelace": 0, "slot": 15},
            ],
            origin_addresses={"a"},
            intermediaries=[],
            prior_cycles=[],
        )
        assert result["amount_similarity"] == 0.0
        assert result["net_loss_ratio"] == 1.0

    def test_a_single_repeated_value_has_positive_zero_entropy(self):
        """Stored in the evidence, where a -0.0 would read as a distinct value."""
        assert math.copysign(1.0, graph._shannon_bits(["a", "a"])) == 1.0


class TestAddressKey:
    def test_a_key_address_is_keyed_on_its_payment_credential(self):
        """The stake part is free to choose, so it must not tell a wallet apart."""
        one = _address(_KEY_BASE_HEADER, _MULE_KEY, _STAKE_1)
        other_stake = _address(_KEY_BASE_HEADER, _MULE_KEY, _STAKE_2)
        enterprise = _address(_KEY_ENTERPRISE_HEADER, _MULE_KEY)
        assert graph._address_key(one) == graph._address_key(other_stake) == _MULE_KEY
        assert graph._address_key(enterprise) == _MULE_KEY

    def test_a_script_address_stays_whole(self):
        """One script hash serves every user of a DEX, so it must not merge them."""
        one = _address(_SCRIPT_BASE_HEADER, _SCRIPT_HASH, _STAKE_1)
        other_owner = _address(_SCRIPT_BASE_HEADER, _SCRIPT_HASH, _STAKE_2)
        assert graph._address_key(one) == one
        assert graph._address_key(one) != graph._address_key(other_owner)


class TestRecycledShare:
    """The window axis credits a cycle for its own reuse only."""

    _HOPS = [
        {"address": "addr_a", "amount_lovelace": 10_000_000, "slot": 100},
        {"address": "addr_b", "amount_lovelace": 9_900_000, "slot": 103},
        {"address": "addr_a", "amount_lovelace": 9_800_000, "slot": 106},
    ]
    _OWN_ADDRESSES = ["addr_a", "addr_b", "addr_c", "addr_a"]

    def _build(self, intermediaries, prior_cycles, addresses=None):
        return _build_cycle_result(
            cycle_length=3,
            addresses=addresses or self._OWN_ADDRESSES,
            origin_amount=10_000_000,
            final_amount=9_800_000,
            hops=self._HOPS,
            origin_addresses={"addr_a"},
            intermediaries=intermediaries,
            prior_cycles=prior_cycles,
        )

    def test_no_history_leaves_the_cycle_s_own_reading(self):
        alone = self._build(["addr_b", "addr_c"], prior_cycles=[])
        assert alone["recycled_share"] == 0.0
        assert alone["recipient_entropy"] == round(1.5 / math.log2(3), 4)

    def test_reusing_every_intermediary_reads_as_fully_concentrated(self):
        again = self._build(["addr_b", "addr_c"], prior_cycles=[(40.0, ["addr_b", "addr_c"])])
        assert again["recycled_share"] == 1.0
        assert again["recipient_entropy"] == 0.0
        assert again["prior_cycles_in_window"] == 1

    def test_reusing_half_the_intermediaries_reads_halfway(self):
        again = self._build(["addr_b", "addr_c"], prior_cycles=[(40.0, ["addr_b"])])
        assert again["recycled_share"] == 0.5
        assert again["recipient_entropy"] == 0.5

    def test_earlier_cycles_through_other_addresses_do_not_dilute_the_reuse(self):
        """Adding cycles through fresh addresses must not lift a recycled ring's
        reading back toward diverse."""
        prior = [(40.0, [f"addr_fresh_{i}"]) for i in range(5)] + [(40.0, ["addr_b", "addr_c"])]
        assert self._build(["addr_b", "addr_c"], prior_cycles=prior)["recycled_share"] == 1.0

    def test_a_concentrated_history_lends_nothing_to_a_cycle_that_reuses_none(self):
        """Ten earlier cycles through one hub say nothing about a cycle that
        goes through none of it: it keeps the reading it has alone."""
        alone = self._build(["addr_y", "addr_z"], prior_cycles=[])
        prior = [(40.0, ["addr_hub"])] * 10
        lent = self._build(["addr_y", "addr_z"], prior_cycles=prior)
        assert lent["recycled_share"] == 0.0
        assert lent["recipient_entropy"] == alone["recipient_entropy"]

    def test_a_mule_with_a_new_stake_part_still_counts_as_reused(self):
        before = _address(_KEY_BASE_HEADER, _MULE_KEY, _STAKE_1)
        now = _address(_KEY_BASE_HEADER, _MULE_KEY, _STAKE_2)
        assert self._build([now], prior_cycles=[(40.0, [before])])["recycled_share"] == 1.0

    def test_the_cycle_s_own_concentration_is_never_raised_by_the_window(self):
        """The lower of the two readings is kept, so a ring whose own addresses
        repeat keeps its per-cycle reading when it reuses nothing."""
        own = ["addr_a", "addr_b", "addr_b", "addr_b", "addr_a"]
        alone = self._build(["addr_b"], prior_cycles=[], addresses=own)
        assert alone["recipient_entropy"] < 1.0, "the ring's own repeats must read as concentrated"
        fresh = [(40.0, [f"addr_fresh_{i}"]) for i in range(3)]
        assert (
            self._build(["addr_b"], prior_cycles=fresh, addresses=own)["recipient_entropy"]
            == alone["recipient_entropy"]
        )


class TestRecurrenceCountsTheSameRing:
    def _recurrence(self, prior_cycles):
        return _build_cycle_result(
            cycle_length=3,
            addresses=["addr_a", "addr_b", "addr_c", "addr_a"],
            origin_amount=10_000_000,
            final_amount=9_800_000,
            hops=TestRecycledShare._HOPS,
            origin_addresses={"addr_a"},
            intermediaries=["addr_b", "addr_c"],
            prior_cycles=prior_cycles,
        )["recurrence_count"]

    def test_an_earlier_high_through_a_shared_intermediary_counts(self):
        assert self._recurrence([(BAND_HIGH_THRESHOLD, ["addr_c", "addr_x"])]) == 1

    def test_an_earlier_high_through_other_addresses_does_not(self):
        """The same origin's High through a different ring is not this cycle
        repeating, so it must not lend recurrence."""
        assert self._recurrence([(BAND_HIGH_THRESHOLD, ["addr_x", "addr_y"])]) == 0

    def test_an_earlier_cycle_below_high_does_not_even_through_the_same_ring(self):
        assert self._recurrence([(_BELOW_HIGH, ["addr_b", "addr_c"])]) == 0


class TestInterHopSpeed:
    def _mean_delta(self, slots):
        hops = [
            {"address": f"addr_{i}", "amount_lovelace": 1, "slot": s} for i, s in enumerate(slots)
        ]
        return _build_cycle_result(
            cycle_length=len(slots),
            addresses=[h["address"] for h in hops],
            origin_amount=1,
            final_amount=1,
            hops=hops,
            origin_addresses={"addr_0"},
            intermediaries=[],
            prior_cycles=[],
        )["mean_inter_hop_delta_slots"]

    def test_a_ring_chained_within_one_block_reads_as_the_fastest(self):
        """Hops sharing a slot are one slot apart at most, not unmeasurable."""
        assert self._mean_delta([100, 100, 100, 100]) == graph._SAME_SLOT_DELTA_SLOTS

    def test_shared_slots_count_toward_the_mean(self):
        assert self._mean_delta([100, 100, 120]) == (graph._SAME_SLOT_DELTA_SLOTS + 20) / 2

    def test_a_hop_at_an_earlier_slot_is_left_out(self):
        assert self._mean_delta([100, 130, 120, 150]) == (30 + 30) / 2


class _SpendsClient:
    """Answers _spenders from a fixed map of transaction -> addresses spent from."""

    def __init__(self, spends, fail=False):
        self._spends = spends
        self._fail = fail
        self.calls = 0

    def execute(self, sql, params=None, **_):
        self.calls += 1
        if self._fail:
            raise ConnectionError("ClickHouse went away")
        if "tx_class_scores" in sql:
            return []
        return [(a,) for tx in params["txs"] for a in self._spends.get(tx, ())]


class TestRingPath:
    """origin tx T0 pays B and a decoy D; T1 (B) pays C and E; T2 (C) closes."""

    _QUERIED = [["addr_b", "addr_d"], ["addr_c", "addr_e"]]
    _CREATOR = {"addr_b": "t0", "addr_d": "t0", "addr_c": "t1", "addr_e": "t1"}

    def test_the_path_is_the_addresses_that_carried_the_value_back(self):
        client = _SpendsClient({"t2": ["addr_c", "addr_other"], "t1": ["addr_b"]})
        path = graph._ring_path(client, "mainnet", "t0", "t2", self._QUERIED, self._CREATOR)
        # The closing spender (C) is in it; the decoy (D) and E never paid back.
        assert path == ["addr_b", "addr_c"]

    def test_every_spender_of_a_path_transaction_is_on_the_path(self):
        client = _SpendsClient({"t2": ["addr_c", "addr_e"], "t1": ["addr_b"]})
        path = graph._ring_path(client, "mainnet", "t0", "t2", self._QUERIED, self._CREATOR)
        assert path == ["addr_b", "addr_c", "addr_e"]


class TestCycleHistory:
    _HOPS = [
        {"address": "addr_a", "amount_lovelace": 10, "slot": 100},
        {"address": "addr_b", "amount_lovelace": 9, "slot": 103},
        {"address": "addr_a", "amount_lovelace": 9, "slot": 106},
    ]

    def _history(self, client, cycle_length):
        return graph._cycle_history(
            client,
            "mainnet",
            origin_tx="t0",
            closing_tx="t2",
            cycle_length=cycle_length,
            origin_addresses={"addr_a"},
            hops=self._HOPS,
            queried=[["addr_b"], ["addr_c"]],
            creator={"addr_b": "t0", "addr_c": "t1"},
        )

    def test_a_closure_the_gate_cannot_score_reads_nothing(self):
        """A two-hop round trip is never scored, so it must not cost a read,
        nor be deferred when one would have failed."""
        client = _SpendsClient({}, fail=True)
        assert self._history(client, graph._MIN_CYCLE_LENGTH - 1) == (["addr_b"], [], False)
        assert client.calls == 0

    def test_a_failed_read_keeps_the_cycle_and_flags_it(self):
        client = _SpendsClient({}, fail=True)
        assert self._history(client, graph._MIN_CYCLE_LENGTH) == (["addr_b"], [], True)


class TestDetectCycle:
    @patch("app.analysis.graph.clickhouse")
    def test_no_inputs_returns_none(self, mock_ch):
        client = MagicMock()
        mock_ch._get_client.return_value = client
        client.execute.return_value = []  # no input addresses
        assert detect_cycle("tx1", "preprod") is None

    @patch("app.analysis.graph.clickhouse")
    def test_no_outputs_returns_none(self, mock_ch):
        client = MagicMock()
        mock_ch._get_client.return_value = client
        # First call: input addresses
        # Second call: output addresses (empty)
        client.execute.side_effect = [
            [("addr_origin",)],
            [],
        ]
        assert detect_cycle("tx1", "preprod") is None

    @patch("app.analysis.graph.clickhouse")
    def test_too_many_recipients_returns_none(self, mock_ch):
        client = MagicMock()
        mock_ch._get_client.return_value = client
        client.execute.side_effect = [
            [("addr_origin",)],
            [(f"addr_{i}", 1_000_000, 100) for i in range(25)],  # 25 recipients
        ]
        assert detect_cycle("tx1", "preprod") is None


class TestRecyclingNeedsValuePassedAlong:
    def test_a_cycle_that_does_not_pass_value_along_gets_no_recycling_credit(self):
        """Hop amounts this far apart are not one sum travelling round: a bot
        routing unrelated amounts through the same hub, which the recycled
        share alone would read as a recycled ring."""
        hops = [
            {"address": "addr_a", "amount_lovelace": 10_000_000, "slot": 100},
            {"address": "addr_b", "amount_lovelace": 1_000_000, "slot": 103},
            {"address": "addr_a", "amount_lovelace": 9_800_000, "slot": 106},
        ]

        def build(prior_cycles):
            return _build_cycle_result(
                cycle_length=3,
                addresses=["addr_a", "addr_b", "addr_c", "addr_a"],
                origin_amount=10_000_000,
                final_amount=9_800_000,
                hops=hops,
                origin_addresses={"addr_a"},
                intermediaries=["addr_b", "addr_c"],
                prior_cycles=prior_cycles,
            )

        alone = build([])
        again = build([(40.0, ["addr_b", "addr_c"])])
        assert again["amount_similarity"] <= graph._VALUE_PRESERVING_SIMILARITY
        assert again["recycled_share"] == 1.0
        assert again["recipient_entropy"] == alone["recipient_entropy"]

    # The similarity is stored at 4 dp, so a bar one step below it is the
    # nearest one the cycle clears.
    _BAR_STEP = 0.0001

    def _ring(self, middle_leg, prior_cycles):
        ada = 1_000_000
        hops = [
            {"address": "addr_a", "amount_lovelace": 10_000 * ada, "slot": 100},
            {"address": "addr_b", "amount_lovelace": middle_leg * ada, "slot": 103},
            {"address": "addr_a", "amount_lovelace": 9_979 * ada, "slot": 106},
        ]
        return _build_cycle_result(
            cycle_length=3,
            addresses=["addr_a", "addr_b", "addr_c", "addr_a"],
            origin_amount=10_000 * ada,
            final_amount=9_979 * ada,
            hops=hops,
            origin_addresses={"addr_a"},
            intermediaries=["addr_b", "addr_c"],
            prior_cycles=prior_cycles,
        )

    def test_the_credit_starts_above_the_configured_similarity(self):
        """At the bar no credit, just above it the reused path counts."""
        reused = [(40.0, ["addr_b", "addr_c"])]
        own = self._ring(8_000, [])
        with patch.object(graph, "_VALUE_PRESERVING_SIMILARITY", own["amount_similarity"]):
            assert self._ring(8_000, reused)["recipient_entropy"] == own["recipient_entropy"]
        just_below = own["amount_similarity"] - self._BAR_STEP
        with patch.object(graph, "_VALUE_PRESERVING_SIMILARITY", just_below):
            assert self._ring(8_000, reused)["recipient_entropy"] == 0.0


class TestClosingReading:
    """The part of the closing leg's payment to the origin set that fits the
    other hops, found without scoring every subset of its outputs."""

    _RING = [10_000_000_000, 9_990_000_000]
    _REPAID = 9_979_000_000
    _CHANGE = 2_000_000_000
    _DUST = 1_000_000
    # Seeded random closing legs checked against every subset sum; small enough
    # to enumerate, varied enough to put the best sum on either side of the peak.
    _CASES = 300
    _MAX_HOPS = 6
    _MAX_OUTPUTS = 8
    _MAX_LOVELACE = 10**10
    # A few legs longer than the old 16-output limit, still enumerable.
    _LONG_CASES = 5
    _LONG_OUTPUTS = 18

    def _best_of_every_subset(self, amounts, prior):
        sums = {
            sum(subset)
            for size in range(1, len(amounts) + 1)
            for subset in itertools.combinations(amounts, size)
        }
        return max(graph._amount_similarity([*prior, c]) for c in sums)

    def _random_leg(self, rng, outputs):
        prior = [rng.randint(1, self._MAX_LOVELACE) for _ in range(rng.randint(2, self._MAX_HOPS))]
        return prior, [rng.randint(1, self._MAX_LOVELACE) for _ in range(outputs)]

    def test_matches_scoring_every_subset_sum(self):
        """Only the nearest sums on each side of the peak are scored, so the
        search must find a reading as good as the best of all of them."""
        rng = random.Random(0)
        for _ in range(self._CASES):
            prior, amounts = self._random_leg(rng, rng.randint(1, self._MAX_OUTPUTS))
            reading = graph._closing_reading(amounts, amounts[0], sum(amounts), prior)
            best = self._best_of_every_subset(amounts, prior)
            assert graph._amount_similarity([*prior, reading]) == best

    def test_finds_the_nearest_sums_past_the_old_limit(self):
        """Legs with more outputs than the old exhaustive limit (16) are still
        searched exactly: the subset sums nearest the target on either side."""
        rng = random.Random(1)
        for _ in range(self._LONG_CASES):
            amounts = [rng.randint(1, self._MAX_LOVELACE) for _ in range(self._LONG_OUTPUTS)]
            target = rng.uniform(0, sum(amounts))
            sums = {0}
            for amount in amounts:
                sums |= {s + amount for s in sums}
            sums.discard(0)
            below = max((s for s in sums if s <= target), default=None)
            above = min((s for s in sums if s >= target), default=None)
            assert graph._nearest_subset_sums(amounts, target) == (below, above)

    def test_a_target_below_every_output_has_no_sum_under_it(self):
        """The empty subset is never a reading."""
        amounts = [self._REPAID, self._CHANGE]
        assert graph._nearest_subset_sums(amounts, self._DUST) == (None, self._CHANGE)

    def test_a_sum_equal_to_the_target_is_found_on_both_sides(self):
        amounts = [self._REPAID, self._CHANGE, self._DUST]
        target = self._REPAID + self._DUST
        assert graph._nearest_subset_sums(amounts, target) == (target, target)

    def test_the_matched_row_is_a_candidate_when_the_reads_disagree(self):
        """If the closing-leg read misses the matched output, the matched row
        still stands: the similarity is never lower than it gave."""
        amounts = [2 * self._REPAID]
        reading = graph._closing_reading(amounts, self._REPAID, sum(amounts), self._RING)
        assert reading == self._REPAID

    def test_a_split_repayment_beside_change_reads_as_the_repayment(self):
        half = self._REPAID // 2
        amounts = [half, half, self._CHANGE]
        assert graph._closing_reading(amounts, half, sum(amounts), self._RING) == 2 * half

    def test_ties_go_to_the_total(self):
        """A closing leg out of all proportion to the other hops scores 0 on
        every reading, the nearest to the peak included: the total is kept."""
        amounts = [self._MAX_LOVELACE, 2 * self._MAX_LOVELACE]
        total = 3 * self._MAX_LOVELACE
        assert graph._closing_reading(amounts, self._MAX_LOVELACE, total, [1, 1]) == total

    def test_past_the_cap_the_part_that_fits_is_assumed_to_be_there(self):
        """Too many outputs to search: the reading is the similarity peak
        itself, within what the leg paid, so splitting the repayment across
        more outputs than the search takes apart cannot lower the similarity."""
        peak = sum(h * h for h in self._RING) / sum(self._RING)
        half = self._REPAID // 2
        split = [half, self._REPAID - half, self._CHANGE, *[self._DUST] * 2]
        short = [self._DUST] * 3
        oversized = [2 * self._REPAID] * 3
        # Every leg below has more outputs than this, so none is searched.
        with patch.object(graph, "_CLOSING_SUBSET_MAX_OUTPUTS", len(short) - 1):
            reading = graph._closing_reading(split, half, sum(split), self._RING)
            assert reading == round(peak)
            best = self._best_of_every_subset(split, self._RING)
            assert graph._amount_similarity([*self._RING, reading]) >= best
            # Never more than the leg paid, never less than one output.
            assert graph._closing_reading(short, self._DUST, sum(short), self._RING) == sum(short)
            assert (
                graph._closing_reading(oversized, oversized[0], sum(oversized), self._RING)
                == oversized[0]
            )
