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
from app.ingestion.ogmios_client import BlockPersistError, OgmiosClient


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
        client._pending_input_cache[tx_hash] = (
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
        assert tx_hash in client._pending_input_cache  # replay keeps enrichment

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
             patch.object(client, "_record_displacements", AsyncMock()), \
             patch.object(client, "_settle_confirmed", AsyncMock()):
            _run(client._handle_roll_forward(_block()))
        assert tx_hash not in client._pending_input_cache


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
        delete = AsyncMock(return_value=3)
        with patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back", AsyncMock()), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()), \
             patch("app.ingestion.ogmios_client.clickhouse.delete_rolled_back_txs_async", delete):
            _run(client._handle_roll_backward(self._result(slot=500)))
        delete.assert_awaited_once_with(client.network, 500)

    def test_rollback_to_origin_skips_purge(self, client, monkeypatch):
        """Rollback-to-origin is a node-resync artifact; purging the whole
        network's history on it would destroy the warehouse."""
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", True)
        delete = AsyncMock()
        with patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back", AsyncMock()), \
             patch("app.ingestion.ogmios_client.clickhouse.delete_rolled_back_txs_async", delete):
            _run(client._handle_roll_backward({"point": "origin", "tip": {"slot": 5}}))
        delete.assert_not_awaited()

    def test_kill_switch(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ROLLBACK_CLEANUP_ENABLED", False)
        delete = AsyncMock()
        with patch("app.ingestion.ogmios_client.postgres.mark_lifecycle_rolled_back", AsyncMock()), \
             patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()), \
             patch("app.ingestion.ogmios_client.clickhouse.delete_rolled_back_txs_async", delete):
            _run(client._handle_roll_backward(self._result()))
        delete.assert_not_awaited()


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
