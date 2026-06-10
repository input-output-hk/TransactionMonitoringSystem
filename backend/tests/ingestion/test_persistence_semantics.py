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
