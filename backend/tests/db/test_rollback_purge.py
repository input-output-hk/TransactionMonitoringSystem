"""Rollback purge: lightweight DELETEs must carry the projection setting.

ClickHouse >= 24.7 rejects lightweight DELETE on a projected table unless
lightweight_mutation_projection_mode is set, so a purge without it crash-loops
chain sync on the first real rollback (live verification:
scripts/verify_rollback_purge.py). These tests pin the setting on every
DELETE and the purge's structural behaviour against a mocked client.
"""

from unittest.mock import MagicMock

import pytest

from app.db import clickhouse
from app.db.clickhouse import (
    _LIGHTWEIGHT_DELETE_SETTINGS,
    _ROLLBACK_CLEANUP_TABLES,
    delete_rolled_back_txs,
)


@pytest.fixture
def client(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(clickhouse, "_get_client", lambda: mock)
    return mock


def _delete_calls(mock):
    return [c for c in mock.execute.call_args_list if "DELETE FROM" in c.args[0]]


class TestLightweightDeleteSettings:
    def test_every_delete_carries_projection_mode(self, client):
        client.execute.side_effect = [[("aa" * 32,)]] + [None] * len(
            _ROLLBACK_CLEANUP_TABLES
        )
        delete_rolled_back_txs("preprod", 100)
        deletes = _delete_calls(client)
        assert len(deletes) == len(_ROLLBACK_CLEANUP_TABLES)
        for call in deletes:
            assert call.kwargs.get("settings") == _LIGHTWEIGHT_DELETE_SETTINGS

    def test_projection_mode_is_rebuild(self):
        # 'drop' would silently degrade reads on mutated parts; 'rebuild'
        # keeps the list-endpoint projection correct on surviving rows.
        assert (
            _LIGHTWEIGHT_DELETE_SETTINGS["lightweight_mutation_projection_mode"]
            == "rebuild"
        )


class TestPurgeStructure:
    def test_score_rows_deleted_last(self, client):
        # tx_class_scores last minimizes the window where a tx has lost its
        # chain facts but still has a score row blocking re-scoring.
        assert _ROLLBACK_CLEANUP_TABLES[-1] == "tx_class_scores"

    def test_no_orphans_short_circuits(self, client):
        client.execute.return_value = []
        result = delete_rolled_back_txs("preprod", 100)
        assert result == []
        assert _delete_calls(client) == []

    def test_returns_orphan_hashes(self, client):
        # The hash list (not a bare count) feeds the delayed score repurge.
        client.execute.side_effect = [[("aa" * 32,), ("bb" * 32,)]] + [None] * len(
            _ROLLBACK_CLEANUP_TABLES
        )
        assert delete_rolled_back_txs("preprod", 100) == ["aa" * 32, "bb" * 32]
