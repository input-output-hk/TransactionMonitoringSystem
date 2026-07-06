"""Checkpoint, retry, rollback-cleanup, and raw_data persistence semantics.

These encode the audit's silent-data-loss findings: a swallowed ClickHouse
insert failure must never advance the sync checkpoint, rollbacks must purge
the analytics warehouse, and an oversized raw payload must be stored as an
honest empty-with-flag, never an invalid JSON prefix.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.db.clickhouse import _serialize_raw_data
from app.ingestion.ogmios_client import (
    BlockPersistError,
    IntersectionNotFoundError,
    OgmiosClient,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client():
    return OgmiosClient()


def _block(slot=100, txs=1):
    return {
        "block": {
            "id": "ab" * 32,
            "slot": slot,
            "height": 7,
            "transactions": [
                {
                    "id": f"{i:02d}" * 32,
                    "spends": "inputs",
                    "fee": {"ada": {"lovelace": 200_000}},
                    "inputs": [{"transaction": {"id": "11" * 32}, "index": 0}],
                    "outputs": [
                        {"address": "addr_test1qq", "value": {"ada": {"lovelace": 1}}}
                    ],
                }
                for i in range(txs)
            ],
        },
        "tip": {"slot": slot + 10},
    }


class TestInsertRetry:
    def test_transient_failure_recovers(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CLICKHOUSE_INSERT_MAX_RETRIES", 3)
        monkeypatch.setattr(settings, "CLICKHOUSE_INSERT_RETRY_BASE_DELAY_SECONDS", 0.0)
        insert = AsyncMock(side_effect=[RuntimeError("down"), None])
        with patch("app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async", insert):
            _run(client._insert_block_with_retry([object()], 100))
        assert insert.await_count == 2

    def test_exhaustion_raises_block_persist_error(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CLICKHOUSE_INSERT_MAX_RETRIES", 2)
        monkeypatch.setattr(settings, "CLICKHOUSE_INSERT_RETRY_BASE_DELAY_SECONDS", 0.0)
        insert = AsyncMock(side_effect=RuntimeError("down"))
        with patch("app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async", insert):
            with pytest.raises(BlockPersistError):
                _run(client._insert_block_with_retry([object()], 100))
        assert insert.await_count == 2


class TestCheckpointNotAdvancedOnFailure:
    def test_failed_insert_never_saves_sync_point(self, client, monkeypatch):
        """The original bug: insert failure was logged and save_sync_point
        still ran, permanently losing the block. The checkpoint write must
        be unreachable when persistence fails."""
        monkeypatch.setattr(settings, "CLICKHOUSE_INSERT_MAX_RETRIES", 1)
        monkeypatch.setattr(settings, "CLICKHOUSE_INSERT_RETRY_BASE_DELAY_SECONDS", 0.0)
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", False)
        save_sync = AsyncMock()
        with patch("app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async",
                   AsyncMock(side_effect=RuntimeError("down"))), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point", save_sync), \
             patch("app.ingestion.ogmios_client.postgres.batch_upsert_lifecycle_confirmed",
                   AsyncMock()):
            with pytest.raises(BlockPersistError):
                _run(client._handle_roll_forward(_block()))
        save_sync.assert_not_awaited()

    def test_empty_block_still_checkpoints(self, client):
        save_sync = AsyncMock()
        with patch("app.ingestion.ogmios_client.postgres.save_sync_point", save_sync):
            _run(client._handle_roll_forward(
                {"block": {"id": "ab" * 32, "slot": 5, "height": 1,
                           "transactions": []},
                 "tip": {"slot": 10}}
            ))
        save_sync.assert_awaited_once()


class TestRawStoreDurability:
    """When RAW_DATA_MAX_BYTES caps the ClickHouse copy, the raw store is
    the ONLY full payload copy: its write must block the checkpoint, and it
    must land before the row becomes engine-visible (review findings)."""

    def _patches(self, raw_write, insert=None, save_sync=None):
        return [
            patch(
                "app.ingestion.ogmios_client.raw_store.write_confirmed",
                raw_write,
            ),
            patch(
                "app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async",
                insert or AsyncMock(),
            ),
            patch(
                "app.ingestion.ogmios_client.postgres.save_sync_point",
                save_sync or AsyncMock(),
            ),
            patch(
                "app.ingestion.ogmios_client.postgres.batch_upsert_lifecycle_confirmed",
                AsyncMock(),
            ),
            patch(
                "app.ingestion.ogmios_client.clickhouse.get_outputs_for_refs_async",
                AsyncMock(return_value={}),
            ),
        ]

    def _run_block(self, client, patches):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            _run(client._handle_roll_forward(_block()))

    def test_raw_write_failure_blocks_checkpoint_when_capped(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", True)
        monkeypatch.setattr(settings, "RAW_DATA_MAX_BYTES", 1)
        save_sync = AsyncMock()
        patches = self._patches(
            AsyncMock(side_effect=OSError("disk full")), save_sync=save_sync,
        )
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with pytest.raises(BlockPersistError):
                _run(client._handle_roll_forward(_block()))
        save_sync.assert_not_awaited()

    def test_raw_write_failure_nonfatal_when_uncapped(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", True)
        monkeypatch.setattr(settings, "RAW_DATA_MAX_BYTES", 0)
        save_sync = AsyncMock()
        self._run_block(client, self._patches(
            AsyncMock(side_effect=OSError("disk full")), save_sync=save_sync,
        ))
        save_sync.assert_awaited_once()

    def test_raw_write_precedes_clickhouse_insert(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", True)
        order = []

        async def raw_write(*a, **k):
            order.append("raw")

        async def insert(*a, **k):
            order.append("insert")

        self._run_block(client, self._patches(raw_write, insert=insert))
        assert order == ["raw", "insert"]


class TestMempoolCacheLifetime:
    """The mempool input enrichment must survive a failed insert (the block
    replays and re-parses) and be dropped only after durable persistence."""

    def _seed(self, client):
        from datetime import datetime, timezone
        tx_hash = "00" * 32  # first tx of _block()
        client.mempool._pending_input_cache[tx_hash] = (
            {("11" * 32, 0): {"address": "addr_test1qsource", "amount": 7}},
            datetime.now(timezone.utc),
        )
        return tx_hash

    def test_cache_survives_failed_insert(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", False)
        monkeypatch.setattr(settings, "CLICKHOUSE_INSERT_MAX_RETRIES", 1)
        monkeypatch.setattr(settings, "CLICKHOUSE_INSERT_RETRY_BASE_DELAY_SECONDS", 0.0)
        tx_hash = self._seed(client)
        with patch("app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async",
                   AsyncMock(side_effect=RuntimeError("down"))), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()), \
             patch("app.ingestion.ogmios_client.clickhouse.get_outputs_for_refs_async",
                   AsyncMock(return_value={})), \
             patch("app.ingestion.ogmios_client.postgres.batch_upsert_lifecycle_confirmed",
                   AsyncMock()):
            with pytest.raises(BlockPersistError):
                _run(client._handle_roll_forward(_block()))
        assert tx_hash in client.mempool._pending_input_cache  # replay keeps enrichment

    def test_cache_popped_after_successful_persist(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", False)
        tx_hash = self._seed(client)
        with patch("app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async",
                   AsyncMock()), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()), \
             patch("app.ingestion.ogmios_client.clickhouse.get_outputs_for_refs_async",
                   AsyncMock(return_value={})), \
             patch("app.ingestion.ogmios_client.postgres.batch_upsert_lifecycle_confirmed",
                   AsyncMock()), \
             patch.object(client.mempool, "record_displacements", AsyncMock()), \
             patch.object(client.mempool, "settle_confirmed", AsyncMock()):
            _run(client._handle_roll_forward(_block()))
        assert tx_hash not in client.mempool._pending_input_cache

    def test_cache_survives_failed_sync_point_save(self, client, monkeypatch):
        """A transient Postgres failure on save_sync_point replays the
        block; popping the enrichment before the checkpoint advanced would
        make the un-enriched replay (fresh ingestion_timestamp) win the
        ReplacingMergeTree merge permanently."""
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", False)
        tx_hash = self._seed(client)
        with patch("app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async",
                   AsyncMock()), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point",
                   AsyncMock(side_effect=RuntimeError("pg blip"))), \
             patch("app.ingestion.ogmios_client.clickhouse.get_outputs_for_refs_async",
                   AsyncMock(return_value={})), \
             patch("app.ingestion.ogmios_client.postgres.batch_upsert_lifecycle_confirmed",
                   AsyncMock()), \
             patch.object(client.mempool, "record_displacements", AsyncMock()), \
             patch.object(client.mempool, "settle_confirmed", AsyncMock()):
            with pytest.raises(RuntimeError):
                _run(client._handle_roll_forward(_block()))
        assert tx_hash in client.mempool._pending_input_cache  # replay keeps enrichment

    def test_cache_still_present_when_sync_point_saves(self, client, monkeypatch):
        """Ordering pin: the pop happens AFTER save_sync_point succeeds,
        so at checkpoint-write time the enrichment must still be cached."""
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", False)
        tx_hash = self._seed(client)
        present_at_save = {}

        async def save_sync(*a, **k):
            present_at_save["cached"] = tx_hash in client.mempool._pending_input_cache

        with patch("app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async",
                   AsyncMock()), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point", save_sync), \
             patch("app.ingestion.ogmios_client.clickhouse.get_outputs_for_refs_async",
                   AsyncMock(return_value={})), \
             patch("app.ingestion.ogmios_client.postgres.batch_upsert_lifecycle_confirmed",
                   AsyncMock()), \
             patch.object(client.mempool, "record_displacements", AsyncMock()), \
             patch.object(client.mempool, "settle_confirmed", AsyncMock()):
            _run(client._handle_roll_forward(_block()))
        assert present_at_save["cached"] is True
        assert tx_hash not in client.mempool._pending_input_cache  # popped after success


class TestStartupValidation:
    def test_capped_payloads_require_raw_store(self, monkeypatch):
        from app.main import _validate_startup_settings

        monkeypatch.setattr(settings, "RAW_DATA_MAX_BYTES", 1024)
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", False)
        with pytest.raises(RuntimeError, match="NO full payload copy"):
            _validate_startup_settings()

    def test_capped_with_store_enabled_passes(self, monkeypatch):
        from app.main import _validate_startup_settings

        monkeypatch.setattr(settings, "RAW_DATA_MAX_BYTES", 1024)
        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", True)
        _validate_startup_settings()  # conftest dev-mode covers the rest


class TestRollbackCleanup:
    def _result(self, slot=500):
        return {"point": {"slot": slot, "id": "cd" * 32}, "tip": {"slot": slot + 5}}

    def test_rollback_purges_clickhouse(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", True)
        delete = AsyncMock(return_value=["aa" * 32, "bb" * 32, "cc" * 32])
        with patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back", AsyncMock()), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()), \
             patch("app.ingestion.ogmios_client.postgres.add_pending_score_repurges", AsyncMock()), \
             patch("app.ingestion.ogmios_client.clickhouse.delete_rolled_back_txs_async", delete):
            _run(client._handle_roll_backward(self._result(slot=500)))
        delete.assert_awaited_once_with(client.network, 500)

    def test_rollback_schedules_delayed_score_repurge(self, client, monkeypatch):
        # The first purge races an in-flight engine batch whose score insert
        # can land just after it; the delayed second tx_class_scores pass
        # clears the stale row that would otherwise block re-scoring forever.
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", True)
        monkeypatch.setattr(settings, "ROLLBACK_SCORE_REPURGE_DELAY_SECONDS", 0)
        hashes = ["aa" * 32]
        repurge = AsyncMock()
        client._running = True

        async def scenario():
            with patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back", AsyncMock()), \
                 patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()), \
                 patch("app.ingestion.ogmios_client.postgres.add_pending_score_repurges", AsyncMock()), \
                 patch("app.ingestion.ogmios_client.postgres.clear_pending_score_repurges", AsyncMock()), \
                 patch("app.ingestion.ogmios_client.clickhouse.delete_rolled_back_txs_async",
                       AsyncMock(return_value=hashes)), \
                 patch("app.ingestion.ogmios_client.clickhouse.delete_score_rows_async", repurge):
                await client._handle_roll_backward(self._result(slot=500))
                # Let the zero-delay repurge task run to completion.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        _run(scenario())
        repurge.assert_awaited_once_with(client.network, hashes)

    def test_no_repurge_scheduled_for_empty_rollback(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", True)
        monkeypatch.setattr(settings, "ROLLBACK_SCORE_REPURGE_DELAY_SECONDS", 0)
        repurge = AsyncMock()

        async def scenario():
            with patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back", AsyncMock()), \
                 patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()), \
                 patch("app.ingestion.ogmios_client.clickhouse.delete_rolled_back_txs_async",
                       AsyncMock(return_value=[])), \
                 patch("app.ingestion.ogmios_client.clickhouse.delete_score_rows_async", repurge):
                await client._handle_roll_backward(self._result(slot=500))
                await asyncio.sleep(0)

        _run(scenario())
        repurge.assert_not_awaited()

    def test_rollback_to_origin_skips_purge_and_lifecycle(self, client, monkeypatch):
        """Rollback-to-origin is a node-resync artifact; purging the whole
        network's history on it would destroy the warehouse AND flipping every
        CONFIRMED row to ROLLED_BACK (slot 0 matches all history) would corrupt
        the lifecycle. Both must be skipped."""
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", True)
        delete = AsyncMock()
        mark = AsyncMock()
        with patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back", mark), \
             patch("app.ingestion.ogmios_client.clickhouse.delete_rolled_back_txs_async", delete):
            _run(client._handle_roll_backward({"point": "origin", "tip": {"slot": 5}}))
        delete.assert_not_awaited()
        mark.assert_not_awaited()  # the whole-history lifecycle wipe is skipped

    def test_rollback_to_slot_still_marks_lifecycle(self, client, monkeypatch):
        """A real (non-origin) rollback must still mark lifecycle rolled-back."""
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", False)  # isolate the mark
        mark = AsyncMock()
        with patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back", mark), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()):
            _run(client._handle_roll_backward(self._result(slot=500)))
        mark.assert_awaited_once_with(500, client.network)

    def test_kill_switch(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", False)
        delete = AsyncMock()
        with patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back", AsyncMock()), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()), \
             patch("app.ingestion.ogmios_client.clickhouse.delete_rolled_back_txs_async", delete):
            _run(client._handle_roll_backward(self._result()))
        delete.assert_not_awaited()


class TestFindIntersection:
    """A resume checkpoint that does not intersect the node's chain must fail
    loudly, not silently re-sync from genesis."""

    def test_intersection_not_found_raises_and_halts(self, client):
        client._replay_pending_score_repurges = AsyncMock()
        next_block = AsyncMock()
        client._handle_roll_forward = next_block  # would be hit if we fell through
        with patch("app.ingestion.ogmios_client.postgres.get_sync_point",
                   AsyncMock(return_value={"slot": 123, "id": "ab" * 32})), \
             patch.object(client, "_send_recv",
                          AsyncMock(return_value={"error": {"code": 1000,
                                                            "message": "IntersectionNotFound"}})):
            with pytest.raises(IntersectionNotFoundError):
                _run(client._chain_sync_loop(object()))
        # Must NOT have advanced to block processing (no genesis replay).
        next_block.assert_not_awaited()


class TestDurableScoreRepurge:
    """The delayed tx_class_scores repurge must be durable: persisted to
    Postgres before the volatile asyncio task is scheduled, cleared only
    after the ClickHouse delete succeeds, replayed on chain-sync
    (re)connect, and strongly referenced so GC cannot drop it. A lost
    repurge leaves a stale score row permanently blocking re-scoring of a
    re-confirmed tx (missed attack)."""

    HASHES = ["aa" * 32, "bb" * 32]

    def _result(self, slot=500):
        return {"point": {"slot": slot, "id": "cd" * 32}, "tip": {"slot": slot + 5}}

    def _patches(self, add_pending=None, clear_pending=None, repurge=None):
        return [
            patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back",
                  AsyncMock()),
            patch("app.ingestion.ogmios_client.postgres.save_sync_point",
                  AsyncMock()),
            patch("app.ingestion.ogmios_client.clickhouse.delete_rolled_back_txs_async",
                  AsyncMock(return_value=list(self.HASHES))),
            patch("app.ingestion.ogmios_client.postgres.add_pending_score_repurges",
                  add_pending or AsyncMock()),
            patch("app.ingestion.ogmios_client.postgres.clear_pending_score_repurges",
                  clear_pending or AsyncMock()),
            patch("app.ingestion.ogmios_client.clickhouse.delete_score_rows_async",
                  repurge or AsyncMock()),
        ]

    def _rollback(self, client, monkeypatch, patches, extra_ticks=2,
                  stop_before_ticks=False):
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", True)

        async def scenario():
            from contextlib import ExitStack
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                await client._handle_roll_backward(self._result())
                if stop_before_ticks:
                    client._running = False
                for _ in range(extra_ticks):
                    await asyncio.sleep(0)

        _run(scenario())

    def test_pending_rows_persisted_before_repurge_runs(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ROLLBACK_SCORE_REPURGE_DELAY_SECONDS", 0)
        order = []
        add_pending = AsyncMock(side_effect=lambda *a, **k: order.append("persist"))
        repurge = AsyncMock(side_effect=lambda *a, **k: order.append("repurge"))
        self._rollback(client, monkeypatch,
                       self._patches(add_pending=add_pending, repurge=repurge))
        add_pending.assert_awaited_once_with(client.network, self.HASHES)
        assert order == ["persist", "repurge"]

    def test_persist_failure_propagates_and_skips_scheduling(self, client, monkeypatch):
        # The persisted row is the durability guarantee; if it cannot be
        # written the rollback handler must fail (connection resets, the
        # node re-sends the rollback) rather than schedule a volatile task.
        monkeypatch.setattr(settings, "ROLLBACK_SCORE_REPURGE_DELAY_SECONDS", 0)
        repurge = AsyncMock()
        with pytest.raises(RuntimeError):
            self._rollback(
                client, monkeypatch,
                self._patches(
                    add_pending=AsyncMock(side_effect=RuntimeError("pg down")),
                    repurge=repurge,
                ),
            )
        repurge.assert_not_awaited()
        assert client._repurge_tasks == set()

    def test_pending_rows_cleared_only_after_repurge_succeeds(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ROLLBACK_SCORE_REPURGE_DELAY_SECONDS", 0)
        order = []
        repurge = AsyncMock(side_effect=lambda *a, **k: order.append("repurge"))
        clear = AsyncMock(side_effect=lambda *a, **k: order.append("clear"))
        self._rollback(client, monkeypatch,
                       self._patches(clear_pending=clear, repurge=repurge),
                       extra_ticks=3)
        clear.assert_awaited_once_with(client.network, self.HASHES)
        assert order == ["repurge", "clear"]

    def test_failed_repurge_keeps_pending_row(self, client, monkeypatch):
        # The row must stay queued for the reconnect replay when the
        # ClickHouse delete fails; clearing it would lose the repurge.
        monkeypatch.setattr(settings, "ROLLBACK_SCORE_REPURGE_DELAY_SECONDS", 0)
        clear = AsyncMock()
        self._rollback(
            client, monkeypatch,
            self._patches(
                clear_pending=clear,
                repurge=AsyncMock(side_effect=RuntimeError("ch down")),
            ),
            extra_ticks=3,
        )
        clear.assert_not_awaited()

    def test_shutdown_inside_delay_window_keeps_pending_row(self, client, monkeypatch):
        # disconnect() inside the delay window: the task returns without
        # repurging, and the persisted row covers the replay on next start.
        #
        # Deterministic regardless of event-loop scheduling: the delayed
        # task is parked on a gate that stands in for the real delay sleep,
        # _running is flipped to False while it is parked, then the gate is
        # released so the task resumes and observes the shutdown. A bare
        # delay of 0 raced under CPython 3.14's task scheduling, where the
        # task could run the repurge during the handler's own post-schedule
        # awaits, before _running was cleared.
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", True)
        # Non-zero so the delayed task parks in the (gated) delay sleep
        # instead of completing inline; the value is irrelevant because the
        # gate, not wall-clock time, drives when the task resumes.
        monkeypatch.setattr(settings, "ROLLBACK_SCORE_REPURGE_DELAY_SECONDS", 3600)
        repurge = AsyncMock()
        clear = AsyncMock()

        real_sleep = asyncio.sleep
        gate = asyncio.Event()

        async def gated_sleep(delay, *args, **kwargs):
            # Park the delayed repurge on the gate; pass the scenario's
            # zero-delay cooperative yields straight through to the loop.
            if delay == 0:
                return await real_sleep(0)
            await gate.wait()

        monkeypatch.setattr(
            "app.ingestion.ogmios_client.asyncio.sleep", gated_sleep
        )

        async def scenario():
            from contextlib import ExitStack
            with ExitStack() as stack:
                for p in self._patches(clear_pending=clear, repurge=repurge):
                    stack.enter_context(p)
                await client._handle_roll_backward(self._result())
                # Shutdown lands while the repurge task is parked in the
                # window; release the gate so it resumes into the _running
                # check rather than the repurge.
                client._running = False
                gate.set()
                for _ in range(3):
                    await asyncio.sleep(0)

        _run(scenario())
        repurge.assert_not_awaited()
        clear.assert_not_awaited()

    def test_task_reference_held_then_discarded(self, client, monkeypatch):
        # A bare create_task result is only weakly referenced by the loop
        # and can be GC'd mid-flight, silently dropping the delayed pass.
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", True)
        monkeypatch.setattr(settings, "ROLLBACK_SCORE_REPURGE_DELAY_SECONDS", 3600)

        async def scenario():
            from contextlib import ExitStack
            with ExitStack() as stack:
                for p in self._patches():
                    stack.enter_context(p)
                await client._handle_roll_backward(self._result())
                assert len(client._repurge_tasks) == 1  # strong ref held
                task = next(iter(client._repurge_tasks))
                task.cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
                # Done-callback discards the reference once the task ends.
                assert client._repurge_tasks == set()

        _run(scenario())

    def test_replay_executes_and_clears_pending(self, client):
        order = []
        get_pending = AsyncMock(return_value=list(self.HASHES))
        repurge = AsyncMock(side_effect=lambda *a, **k: order.append("repurge"))
        clear = AsyncMock(side_effect=lambda *a, **k: order.append("clear"))
        with patch("app.ingestion.ogmios_client.postgres.get_pending_score_repurges",
                   get_pending), \
             patch("app.ingestion.ogmios_client.clickhouse.delete_score_rows_async",
                   repurge), \
             patch("app.ingestion.ogmios_client.postgres.clear_pending_score_repurges",
                   clear):
            _run(client._replay_pending_score_repurges())
        repurge.assert_awaited_once_with(client.network, self.HASHES)
        clear.assert_awaited_once_with(client.network, self.HASHES)
        assert order == ["repurge", "clear"]

    def test_replay_noop_when_nothing_pending(self, client):
        repurge = AsyncMock()
        with patch("app.ingestion.ogmios_client.postgres.get_pending_score_repurges",
                   AsyncMock(return_value=[])), \
             patch("app.ingestion.ogmios_client.clickhouse.delete_score_rows_async",
                   repurge):
            _run(client._replay_pending_score_repurges())
        repurge.assert_not_awaited()

    def test_replay_failure_keeps_rows_and_does_not_crash_sync(self, client):
        # The reconnect path IS the retry mechanism: a failed replay leaves
        # the rows queued and must not take down the chain-sync loop.
        clear = AsyncMock()
        with patch("app.ingestion.ogmios_client.postgres.get_pending_score_repurges",
                   AsyncMock(return_value=list(self.HASHES))), \
             patch("app.ingestion.ogmios_client.clickhouse.delete_score_rows_async",
                   AsyncMock(side_effect=RuntimeError("ch down"))), \
             patch("app.ingestion.ogmios_client.postgres.clear_pending_score_repurges",
                   clear):
            _run(client._replay_pending_score_repurges())  # must not raise
        clear.assert_not_awaited()

    def test_chain_sync_connect_replays_before_processing(self, client):
        # Restart/reconnect inside the delay window: the persisted repurge
        # must run before any chain messages are exchanged.
        order = []

        async def replay():
            order.append("replay")

        async def send_recv(ws, method, params=None):
            order.append(method)
            return {"result": {}}

        client._running = False  # skip the nextBlock loop body
        with patch.object(client, "_replay_pending_score_repurges", replay), \
             patch.object(client, "_send_recv", send_recv), \
             patch("app.ingestion.ogmios_client.postgres.get_sync_point",
                   AsyncMock(return_value=None)):
            _run(client._chain_sync_loop(ws=object()))
        assert order and order[0] == "replay"


class TestSerializeRawData:
    def test_full_payload_stored_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "RAW_DATA_MAX_BYTES", 0)
        payload = {"outputs": [{"datum": "ff" * 50_000}]}
        raw_json, truncated = _serialize_raw_data(payload)
        assert truncated == 0
        import json
        assert json.loads(raw_json) == payload  # round-trips: valid JSON

    def test_oversized_payload_stored_empty_with_flag(self, monkeypatch):
        """The original bug stored a mid-JSON slice: unparseable, so every
        raw_data-gated scorer silently skipped the (attack-shaped) tx."""
        monkeypatch.setattr(settings, "RAW_DATA_MAX_BYTES", 100)
        raw_json, truncated = _serialize_raw_data({"d": "x" * 500})
        assert raw_json == ""
        assert truncated == 1

    def test_absent_payload(self):
        assert _serialize_raw_data(None) == ("", 0)
