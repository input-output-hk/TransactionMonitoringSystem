"""The scoring lanes' poll and backlog client against a live ClickHouse.

The hermetic suite mocks client.execute, so it checks the poll's SQL shape, not
what the server returns for it: a NOT IN that never matched, or a hydrate read
that dropped rows, would pass there and silently stop scoring here. Each test
writes into its own randomised stretch of ingestion time in the LIVE_NETWORK
namespace, so leftovers of other runs cannot fall inside its range. Requires
TMS_LIVE_DB_TESTS=1 (see conftest).
"""

import json
import random
import uuid
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.db import clickhouse
from app.db import clickhouse_scores as scores

from .conftest import LIVE_NETWORK

# Ingestion time the randomised ranges are drawn from: far behind any real row.
_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
_SPREAD_MINUTES = 10_000_000
_RANGE = timedelta(hours=1)
_BATCH = 50


def _hash() -> str:
    return uuid.uuid4().hex * 2


def _naive(dt: datetime) -> datetime:
    # The driver binds naive datetimes as UTC.
    return dt.astimezone(UTC).replace(tzinfo=None)


class _Range:
    """An hour of ingestion time no other run writes into."""

    def __init__(self) -> None:
        self.start = _EPOCH + timedelta(minutes=random.randrange(_SPREAD_MINUTES))
        self.end = self.start + _RANGE

    def at(self, minutes: int) -> datetime:
        return self.start + timedelta(minutes=minutes)

    def poll(self, **kwargs) -> list[dict]:
        return scores.get_unanalyzed_transactions(
            LIVE_NETWORK, _BATCH, since=self.start, until=self.end, **kwargs
        )


def _insert_tx(tx_hash, ingested, input_count=0, raw_data="{}", network=LIVE_NETWORK):
    clickhouse._get_client().execute(
        "INSERT INTO transactions (tx_hash, network, timestamp, fee, input_count, "
        "raw_data, ingestion_timestamp) VALUES",
        [(tx_hash, network, _naive(ingested), 1, input_count, raw_data, _naive(ingested))],
    )


def _insert_inputs(tx_hash, ingested):
    # The writer stamps a tx's inputs with the tx's own ingestion time.
    clickhouse._get_client().execute(
        "INSERT INTO transaction_inputs (tx_hash, network, input_index, address, "
        "ingestion_timestamp) VALUES",
        [(tx_hash, LIVE_NETWORK, 0, "addr_test1qqlivepoll", _naive(ingested))],
    )


def _insert_score(tx_hash, analyzed):
    clickhouse._get_client().execute(
        "INSERT INTO tx_class_scores (tx_hash, network, max_score, max_class, risk_band, "
        "sub_scores, evidence, analysis_version, analyzed_at) VALUES",
        [
            (
                tx_hash,
                LIVE_NETWORK,
                0.0,
                "",
                "Informational",
                "{}",
                "{}",
                "live-db",
                _naive(analyzed),
            )
        ],
    )


class TestPoll:
    def test_it_returns_what_is_unscored(self, ch):
        r = _Range()
        unscored, scored, inputs_gone, treasury = (_hash() for _ in range(4))
        _insert_tx(unscored, r.at(1), input_count=1)
        _insert_inputs(unscored, r.at(1))
        _insert_tx(scored, r.at(2), input_count=1)
        _insert_inputs(scored, r.at(2))
        _insert_score(scored, r.at(3))
        # Its inputs rows are gone (expired by retention before it was scored):
        # still picked, never passed over. The writer commits transactions
        # last, so a visible row is never one whose inputs are on their way.
        _insert_tx(inputs_gone, r.at(3), input_count=1)
        # No inputs to wait for.
        _insert_tx(treasury, r.at(4))

        assert {row["tx_hash"] for row in r.poll()} == {unscored, inputs_gone, treasury}

    def test_the_lanes_read_opposite_ends(self, ch):
        r = _Range()
        older, newer = _hash(), _hash()
        _insert_tx(older, r.at(1))
        _insert_tx(newer, r.at(2))
        oldest_first = [row["tx_hash"] for row in r.poll()]
        newest_first = [row["tx_hash"] for row in r.poll(newest_first=True)]
        assert oldest_first == [older, newer]
        assert newest_first == [newer, older]

    def test_nothing_outside_the_range_comes_back(self, ch):
        r = _Range()
        inside, after = _hash(), _hash()
        _insert_tx(inside, r.at(1))
        _insert_tx(after, r.end)  # until is exclusive
        assert [row["tx_hash"] for row in r.poll()] == [inside]

    def test_a_re_ingested_tx_comes_back_once_in_its_newest_copy(self, ch):
        r = _Range()
        tx = _hash()
        _insert_tx(tx, r.at(1), raw_data=json.dumps({"copy": 1}))
        _insert_tx(tx, r.at(2), raw_data=json.dumps({"copy": 2}))
        rows = r.poll()
        assert len(rows) == 1
        assert json.loads(rows[0]["raw_data"]) == {"copy": 2}

    def test_the_batch_comes_back_with_its_payload(self, ch):
        r = _Range()
        tx = _hash()
        _insert_tx(tx, r.at(1), raw_data=json.dumps({"inputs": [], "outputs": []}))
        (row,) = r.poll()
        assert json.loads(row["raw_data"]) == {"inputs": [], "outputs": []}
        assert row["ingestion_timestamp"] == _naive(r.at(1))


class TestStoredCopies:
    """What the engine checks a batch against before storing its scores."""

    def _insert_copy(self, tx_hash, block_hash, ingested):
        clickhouse._get_client().execute(
            "INSERT INTO transactions (tx_hash, network, timestamp, fee, raw_data, block_hash, "
            "ingestion_timestamp) VALUES",
            [(tx_hash, LIVE_NETWORK, _naive(ingested), 1, "{}", block_hash, _naive(ingested))],
        )

    def test_a_purged_copy_is_gone_and_its_replacement_told_apart(self, ch):
        """A slot battle: the purge and the re-confirmation in another block can
        land in one second, so only the block hash tells the two copies apart,
        and the projection must not serve the purged one."""
        r = _Range()
        tx = _hash()
        purged, current = "aa" * 32, "bb" * 32
        self._insert_copy(tx, purged, r.at(1))
        (hydrated,) = r.poll()
        assert hydrated["block_hash"] == purged

        clickhouse._get_client().execute(
            "DELETE FROM transactions WHERE network = %(network)s AND tx_hash = %(tx)s",
            {"network": LIVE_NETWORK, "tx": tx},
            settings=clickhouse._LIGHTWEIGHT_DELETE_SETTINGS,
        )
        self._insert_copy(tx, current, r.at(1))

        assert scores.stored_copies(LIVE_NETWORK, [tx]) == {(tx, current, _naive(r.at(1)))}
        held = (tx, hydrated["block_hash"], hydrated["ingestion_timestamp"])
        assert held not in scores.stored_copies(LIVE_NETWORK, [tx])


class TestEarliestIngestion:
    def test_it_reads_the_first_stamp_and_none_before_any(self, ch):
        network = f"{LIVE_NETWORK}-{uuid.uuid4().hex[:8]}"
        assert scores.earliest_ingestion(network) is None
        first = _EPOCH + timedelta(days=1)
        _insert_tx(_hash(), first + _RANGE, network=network)
        _insert_tx(_hash(), first, network=network)
        assert scores.earliest_ingestion(network) == first


class TestBacklogClient:
    def test_backlog_queries_reach_the_server_thread_capped(self, ch):
        """Read back from system.query_log: the driver sends settings as
        not-important, so a cap the server did not apply would not raise."""
        marker = f"backlog-cap-{uuid.uuid4().hex[:12]}"
        clickhouse._ch_backlog_executor.submit(
            lambda: clickhouse._get_client().execute("SELECT %(m)s", {"m": marker})
        ).result()
        ch._execute_query("SYSTEM FLUSH LOGS")
        rows = ch._execute_query(
            """
            SELECT Settings['max_threads'] FROM system.query_log
            WHERE type = 'QueryFinish' AND query LIKE %(pattern)s
              AND query NOT LIKE '%%system.query_log%%'
            """,
            {"pattern": f"%{marker}%"},
        )
        assert rows == [(str(settings.ANALYSIS_BACKLOG_MAX_THREADS),)], rows


class TestBacklogLaneEndToEnd:
    """The real lane over real rows: pick, score, insert, pick again, advance.

    Nothing mocked between the tick and the servers, so a score the pick could
    not see (the lane would then re-score it forever, or stop) or a checkpoint
    that moved past a transaction would show here.
    """

    def test_it_scores_what_aged_out_and_checkpoints_past_it(self, ch, pg_run, monkeypatch):
        from app.analysis import engine
        from app.db import postgres
        from app.db.postgres import get_connection
        from app.tasks import analysis

        network = f"{LIVE_NETWORK}-{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)
        window = timedelta(seconds=settings.ANALYSIS_LIVE_WINDOW_SECONDS)
        settle = timedelta(seconds=settings.ANALYSIS_BACKLOG_SETTLE_SECONDS)
        # Out of the live window, one of them inside the settle margin.
        stamps = [now - window - settle - timedelta(minutes=m) for m in (30, 20)]
        stamps.append(now - window - timedelta(minutes=5))
        hashes = [_hash() for _ in stamps]
        for tx_hash, stamp in zip(hashes, stamps, strict=True):
            _insert_tx(
                tx_hash, stamp, raw_data=json.dumps({"inputs": [], "outputs": []}), network=network
            )
        start = min(stamps) - timedelta(minutes=1)
        monkeypatch.setattr(analysis, "_scoring", analysis._ScoringState())
        # Otherwise collision enrichment keeps a handle on this test's event
        # loop after pg_run closes it; without one it skips, which this test
        # does not need.
        monkeypatch.setattr(engine, "set_main_loop", lambda loop: None)

        async def scenario():
            async with get_connection() as conn:
                await conn.execute("DELETE FROM scoring_checkpoint WHERE network = $1", network)
            try:
                await postgres.save_scoring_checkpoint(network, start)
                await analysis._backlog_tick(network)
                return await postgres.get_scoring_checkpoint(network)
            finally:
                async with get_connection() as conn:
                    await conn.execute("DELETE FROM scoring_checkpoint WHERE network = $1", network)

        checkpoint = pg_run(scenario)

        scored = ch._execute_query(
            "SELECT tx_hash FROM tx_class_scores WHERE network = %(network)s",
            {"network": network},
        )
        assert {row[0] for row in scored} == set(hashes)
        # Past every transaction it checked, short of the settle margin.
        assert max(stamps[:2]) < checkpoint <= datetime.now(UTC) - window - settle
        assert analysis._scoring.oldest_unscored is None


class TestRollbackRepurgeBehindTheCheckpoint:
    """A pending score repurge replayed after the checkpoint passed its transaction.

    The rollback repurge is persisted and replayed at the next chain-sync
    (re)connect, which can be hours after a rolled-back transaction re-confirmed,
    was scored, and had its slice checked by the backlog lane. Neither lane
    reads that ingestion time again, so a score the replay deleted would be gone
    for good: the replay must keep what was scored from the re-confirmed copy.
    """

    def test_a_late_replay_keeps_the_re_confirmed_copy_s_score(self, ch, pg_run, monkeypatch):
        from app.analysis import engine
        from app.db import postgres
        from app.db.postgres import get_connection
        from app.ingestion.ogmios_client import OgmiosClient
        from app.tasks import analysis

        network = f"{LIVE_NETWORK}-{uuid.uuid4().hex[:8]}"
        reconfirmed, vanished = _hash(), _hash()
        # Re-confirmed an hour before the settle margin: behind the checkpoint
        # once the lane ticks.
        settled = timedelta(
            seconds=settings.ANALYSIS_LIVE_WINDOW_SECONDS + settings.ANALYSIS_BACKLOG_SETTLE_SECONDS
        )
        stamp = datetime.now(UTC) - settled - timedelta(hours=1)
        fresh = stamp + timedelta(seconds=5)
        # Written by a batch that still held the rolled-back copy.
        stale = stamp - timedelta(minutes=1)
        _insert_tx(
            reconfirmed, stamp, raw_data=json.dumps({"inputs": [], "outputs": []}), network=network
        )
        for tx_hash, analyzed in ((reconfirmed, fresh), (reconfirmed, stale), (vanished, stale)):
            clickhouse._get_client().execute(
                "INSERT INTO tx_class_scores (tx_hash, network, max_score, max_class, risk_band, "
                "sub_scores, evidence, analysis_version, analyzed_at) VALUES",
                [
                    (
                        tx_hash,
                        network,
                        0.0,
                        "",
                        "Informational",
                        "{}",
                        "{}",
                        "live-db",
                        _naive(analyzed),
                    )
                ],
            )
        monkeypatch.setattr(analysis, "_scoring", analysis._ScoringState())
        monkeypatch.setattr(engine, "set_main_loop", lambda loop: None)

        async def _clear(conn):
            await conn.execute("DELETE FROM scoring_checkpoint WHERE network = $1", network)
            await conn.execute("DELETE FROM pending_score_repurges WHERE network = $1", network)

        async def scenario():
            async with get_connection() as conn:
                await _clear(conn)
            try:
                await postgres.add_pending_score_repurges(network, [reconfirmed, vanished])
                await postgres.save_scoring_checkpoint(network, stamp - timedelta(minutes=1))
                await analysis._backlog_tick(network)
                checkpoint = await postgres.get_scoring_checkpoint(network)
                ogmios = OgmiosClient()
                ogmios.network = network
                await ogmios._replay_pending_score_repurges()
                return checkpoint, await postgres.get_pending_score_repurges(network)
            finally:
                async with get_connection() as conn:
                    await _clear(conn)

        checkpoint, pending = pg_run(scenario)

        assert checkpoint > stamp, "the precondition: the lane has passed the re-confirmed copy"
        assert pending == []
        rows = ch._execute_query(
            "SELECT tx_hash, analyzed_at FROM tx_class_scores WHERE network = %(network)s",
            {"network": network},
        )
        # The vanished tx and the stale row go; the re-confirmed copy's score stays.
        assert rows == [(reconfirmed, _naive(fresh).replace(microsecond=0))]
