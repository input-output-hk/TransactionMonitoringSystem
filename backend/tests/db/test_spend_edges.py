"""spend_edges: the rows the block writer makes, and the backfill's resumption.

The circular BFS sees value move only through these rows, so an edge the writer
leaves out is a cycle leg no hop finds, and one it adds is a leg that never
moved value. The live tier checks the same rows against the backfill and the
hop query; this pins the writer's rules and the backfill driver hermetically.
"""

import logging
from datetime import UTC, datetime
from itertools import pairwise

import pytest

from app.db import spend_edges
from app.models.transaction import NormalizedTransaction, TransactionInput, TransactionOutput

_NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
_SLOT = 1_000
_SPENT = "ef" * 32


def _tx(inputs, outputs, slot=_SLOT, tx_hash="ab" * 32) -> NormalizedTransaction:
    return NormalizedTransaction(
        tx_hash=tx_hash,
        network="preprod",
        slot=slot,
        timestamp=_NOW,
        fee=1,
        inputs=inputs,
        outputs=outputs,
        input_count=len(inputs),
        output_count=len(outputs),
        addresses=[],
    )


def _in(address, **flags) -> TransactionInput:
    return TransactionInput(tx_hash=_SPENT, index=0, address=address, amount=1, **flags)


def _out(address, amount, **flags) -> TransactionOutput:
    return TransactionOutput(address=address, amount=amount, **flags)


def _edges(*txs) -> set[tuple]:
    return {row[1:6] for row in spend_edges.rows_for(list(txs), _NOW)}


class TestRowsFor:
    def test_every_spender_reaches_every_payee(self):
        tx = _tx([_in("a1"), _in("a2")], [_out("b", 5), _out("c", 7)])
        assert _edges(tx) == {
            ("a1", _SLOT, tx.tx_hash, "b", 5),
            ("a1", _SLOT, tx.tx_hash, "c", 7),
            ("a2", _SLOT, tx.tx_hash, "b", 5),
            ("a2", _SLOT, tx.tx_hash, "c", 7),
        }

    @pytest.mark.attack_must_fire
    def test_equal_outputs_to_one_address_count_in_full(self):
        """A leg paid as equal outputs must not read as one of them: an uneven
        ring for the price of an extra output (see graph._HOP_QUERY)."""
        tx = _tx([_in("a")], [_out("b", 5), _out("b", 5)])
        assert _edges(tx) == {("a", _SLOT, tx.tx_hash, "b", 10)}

    def test_inputs_that_move_no_value_are_not_spends(self):
        tx = _tx(
            [
                _in("a"),
                _in("collateral", is_collateral=True),
                _in("reference", is_reference=True),
                _in("failed", is_unspent_attempt=True),
                _in(""),  # never resolved: no hop can follow it
            ],
            [_out("b", 5)],
        )
        assert {edge[0] for edge in _edges(tx)} == {"a"}

    def test_a_collateral_return_is_not_a_payment(self):
        tx = _tx([_in("a")], [_out("b", 5), _out("a", 3, is_collateral=True)])
        assert _edges(tx) == {("a", _SLOT, tx.tx_hash, "b", 5)}

    def test_a_slotless_mempool_copy_has_no_edges(self):
        # No hop window holds it; its confirming block writes the edges.
        assert _edges(_tx([_in("a")], [_out("b", 5)], slot=None)) == set()

    def test_rows_carry_the_network_and_the_ingestion_stamp(self):
        stamp = datetime(2026, 11, 1, tzinfo=UTC)
        tx = _tx([_in("a")], [_out("b", 5)]).model_copy(update={"ingestion_timestamp": stamp})
        [row] = spend_edges.rows_for([tx], _NOW)
        assert row[0] == "preprod" and row[-1] == stamp
        assert dict(zip(spend_edges.EDGE_COLUMNS, row, strict=True))["paid"] == 5


class TestChunks:
    def test_the_chunks_tile_the_hex_space_in_order(self):
        bounds = [spend_edges._chunk_bounds(c) for c in range(spend_edges.BACKFILL_CHUNKS)]
        assert bounds[0][0] == "00"
        for (_, hi), (lo, _) in pairwise(bounds):
            assert hi == lo
        assert all(digit < bounds[-1][1] for digit in "0123456789abcdef")


class _Progress:
    """Stands in for the Postgres progress row."""

    def __init__(self, next_chunk=0, completed=False):
        self.next_chunk, self.completed = next_chunk, completed
        self.saved: list[tuple[int, bool]] = []

    async def get(self, network):
        return self.next_chunk, self.completed

    async def save(self, network, next_chunk, completed):
        self.saved.append((next_chunk, completed))
        self.next_chunk, self.completed = next_chunk, completed


@pytest.fixture
def progress(monkeypatch):
    from app.db import postgres

    state = _Progress()
    monkeypatch.setattr(postgres, "get_spend_edges_backfill", state.get)
    monkeypatch.setattr(postgres, "save_spend_edges_backfill", state.save)
    return state


@pytest.fixture
def filled(monkeypatch):
    chunks: list[int] = []
    monkeypatch.setattr(spend_edges, "backfill_chunk", lambda network, chunk: chunks.append(chunk))
    return chunks


class _Coverage:
    """Stands in for missing_chunks: one answer per check, then nothing missing."""

    def __init__(self, *answers: list[int]):
        self.answers = list(answers)
        self.checks: list[datetime] = []

    def __call__(self, network, before):
        self.checks.append(before)
        return self.answers.pop(0) if self.answers else []


@pytest.fixture
def coverage(monkeypatch):
    def install(*answers: list[int]) -> _Coverage:
        check = _Coverage(*answers)
        monkeypatch.setattr(spend_edges, "missing_chunks", check)
        return check

    return install


_LAST = spend_edges.BACKFILL_CHUNKS - 1


class TestEnsureBackfilled:
    async def test_a_restart_resumes_after_the_last_recorded_chunk(self, progress, filled):
        progress.next_chunk = spend_edges.BACKFILL_CHUNKS - 2
        await spend_edges.ensure_backfilled("preprod", lambda: False)
        assert filled == [spend_edges.BACKFILL_CHUNKS - 2, _LAST]
        assert progress.saved[-1] == (spend_edges.BACKFILL_CHUNKS, True)

    async def test_a_completed_backfill_is_not_run_again(self, progress, filled):
        progress.completed = True
        assert await spend_edges.ensure_backfilled("preprod", lambda: False) is None
        assert filled == []

    async def test_without_a_start_time_the_history_is_not_checked(
        self, progress, filled, coverage
    ):
        progress.completed = True
        check = coverage([3])
        assert await spend_edges.ensure_backfilled("preprod", lambda: False) is None
        assert check.checks == [] and filled == []

    async def test_a_whole_completed_history_is_not_filled_again(self, progress, filled, coverage):
        progress.completed = True
        check = coverage([])
        missing = await spend_edges.ensure_backfilled("preprod", lambda: False, history_before=_NOW)
        assert missing == 0
        assert check.checks == [_NOW] and filled == []

    @pytest.mark.attack_must_fire
    async def test_a_completed_history_missing_chunks_refills_exactly_those(
        self, progress, filled, coverage, caplog
    ):
        """Postgres says complete over a ClickHouse restored apart from it:
        scoring on the holes would miss every cycle leg they held, for good."""
        progress.next_chunk, progress.completed = spend_edges.BACKFILL_CHUNKS, True
        check = coverage([3, 7])
        with caplog.at_level(logging.ERROR, logger=spend_edges.logger.name):
            missing = await spend_edges.ensure_backfilled(
                "preprod", lambda: False, history_before=_NOW
            )
        assert filled == [3, 7]
        assert check.checks == [_NOW, _NOW], "verified once more after the refill"
        assert missing == 0
        assert "[3, 7]" in caplog.text
        assert progress.saved == [], "the progress row stays the resume hint"

    async def test_a_resumed_fill_is_checked_at_its_end(self, progress, filled, coverage):
        # A backup taken during the fill restores Postgres at a later chunk over
        # an empty spend_edges: the chunks before it were never filled again.
        progress.next_chunk = _LAST
        check = coverage([0, 1])
        missing = await spend_edges.ensure_backfilled("preprod", lambda: False, history_before=_NOW)
        assert filled == [_LAST, 0, 1]
        assert check.checks == [_NOW, _NOW]
        assert missing == 0

    async def test_a_fresh_fill_is_checked_at_its_end(self, progress, filled, coverage):
        check = coverage([])
        missing = await spend_edges.ensure_backfilled("preprod", lambda: False, history_before=_NOW)
        assert filled == list(range(spend_edges.BACKFILL_CHUNKS))
        assert check.checks == [_NOW] and missing == 0

    async def test_a_gap_the_refill_leaves_is_reported_not_looped_on(
        self, progress, filled, coverage, caplog
    ):
        # Holding scoring until the graph is whole would stop all detection.
        progress.completed = True
        check = coverage([3, 7], [7])
        with caplog.at_level(logging.ERROR, logger=spend_edges.logger.name):
            missing = await spend_edges.ensure_backfilled(
                "preprod", lambda: False, history_before=_NOW
            )
        assert filled == [3, 7]
        assert len(check.checks) == 2
        assert missing == 1
        assert "still have spends without their edges" in caplog.text

    async def test_progress_is_reported_after_each_chunk(self, progress, filled):
        reported: list[int] = []
        progress.next_chunk = spend_edges.BACKFILL_CHUNKS - 2
        await spend_edges.ensure_backfilled("preprod", lambda: False, on_progress=reported.append)
        assert reported == [_LAST, spend_edges.BACKFILL_CHUNKS]

    async def test_progress_is_reported_through_a_refill(self, progress, filled, coverage):
        # A refill that stops advancing must show as a stall, as a fill does.
        progress.completed = True
        coverage([3, 7])
        reported: list[int] = []
        await spend_edges.ensure_backfilled(
            "preprod", lambda: False, history_before=_NOW, on_progress=reported.append
        )
        chunks = spend_edges.BACKFILL_CHUNKS
        assert reported == [chunks - 2, chunks - 1, chunks]

    async def test_a_stop_leaves_the_rest_for_the_next_start(self, progress, filled):
        await spend_edges.ensure_backfilled("preprod", lambda: len(filled) == 1)
        assert filled == [0]
        assert progress.saved == [(1, False)]

    async def test_a_stop_during_a_refill_reports_no_check(self, progress, filled, coverage):
        progress.completed = True
        coverage([3, 7])
        missing = await spend_edges.ensure_backfilled(
            "preprod", lambda: len(filled) == 1, history_before=_NOW
        )
        assert filled == [3]
        assert missing is None

    async def test_a_failed_chunk_is_retried_not_skipped(self, progress, monkeypatch):
        from app.config import settings

        attempts: list[int] = []

        def flaky(network, chunk):
            attempts.append(chunk)
            if len(attempts) == 1:
                raise ConnectionError("ClickHouse went away")

        monkeypatch.setattr(spend_edges, "backfill_chunk", flaky)
        monkeypatch.setattr(settings, "ANALYSIS_ENGINE_INTERVAL_SECONDS", 0)
        progress.next_chunk = _LAST
        await spend_edges.ensure_backfilled("preprod", lambda: False)
        assert attempts == [_LAST] * 2
        assert progress.completed

    async def test_a_failed_check_is_retried(self, progress, filled, monkeypatch):
        from app.config import settings

        checks: list[datetime] = []

        def flaky(network, before):
            checks.append(before)
            if len(checks) == 1:
                raise ConnectionError("ClickHouse went away")
            return []

        monkeypatch.setattr(spend_edges, "missing_chunks", flaky)
        monkeypatch.setattr(settings, "ANALYSIS_ENGINE_INTERVAL_SECONDS", 0)
        progress.completed = True
        missing = await spend_edges.ensure_backfilled("preprod", lambda: False, history_before=_NOW)
        assert len(checks) == 2 and missing == 0

    async def test_a_check_failing_every_time_lets_scoring_start(
        self, progress, filled, monkeypatch, caplog
    ):
        """Unlike the fill: the history is complete as far as its record goes,
        and holding both lanes on the check would stop every detection."""
        import logging

        from app.config import settings

        checks: list[datetime] = []

        def failing(network, before):
            checks.append(before)
            raise MemoryError("pass over the memory limit")

        monkeypatch.setattr(spend_edges, "missing_chunks", failing)
        monkeypatch.setattr(settings, "ANALYSIS_ENGINE_INTERVAL_SECONDS", 0)
        progress.completed = True
        with caplog.at_level(logging.ERROR, logger=spend_edges.logger.name):
            missing = await spend_edges.ensure_backfilled(
                "preprod", lambda: False, history_before=_NOW
            )
        assert missing is None
        assert len(checks) == settings.SPEND_EDGES_CHECK_ATTEMPTS
        assert "scoring starts without it" in caplog.text
