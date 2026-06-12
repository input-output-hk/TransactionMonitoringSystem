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
    def test_transactions_deleted_last(self, client):
        # transactions is the table the orphan hashes are SELECTed from: if
        # it were deleted before another table whose DELETE then failed, the
        # retry would re-select nothing and the other tables would keep
        # orphans forever (a stale tx_class_scores row blocks re-scoring).
        assert _ROLLBACK_CLEANUP_TABLES[-1] == "transactions"

    def test_score_rows_deleted_second_to_last(self, client):
        # tx_class_scores as late as possible minimizes the window where an
        # in-flight engine batch re-inserts a score row after its purge.
        assert _ROLLBACK_CLEANUP_TABLES[-2] == "tx_class_scores"

    def test_deletes_issued_in_declared_order(self, client):
        client.execute.side_effect = [[("aa" * 32,)]] + [None] * len(
            _ROLLBACK_CLEANUP_TABLES
        )
        delete_rolled_back_txs("preprod", 100)
        deleted_tables = [
            c.args[0].split("DELETE FROM ")[1].split(" ")[0]
            for c in _delete_calls(client)
        ]
        assert deleted_tables == list(_ROLLBACK_CLEANUP_TABLES)

    def test_partial_failure_is_idempotent_on_retry(self, client):
        """An EARLY table DELETE failing must leave the hash source
        (transactions) untouched, so a re-run re-selects the same hashes
        and deletes the remainder. Before the reorder, transactions was
        deleted first and a partial failure orphaned six tables forever."""
        hashes = [("aa" * 32,)]

        # First run: the very first table DELETE fails.
        client.execute.side_effect = [hashes, RuntimeError("CH hiccup")]
        with pytest.raises(RuntimeError):
            delete_rolled_back_txs("preprod", 100)
        # The transactions DELETE was never reached: the hash source survives.
        assert not any(
            "DELETE FROM transactions" in c.args[0]
            for c in _delete_calls(client)
        )

        # Retry: the source still yields the hashes; every table is purged.
        client.execute.reset_mock()
        client.execute.side_effect = [hashes] + [None] * len(
            _ROLLBACK_CLEANUP_TABLES
        )
        assert delete_rolled_back_txs("preprod", 100) == ["aa" * 32]
        assert len(_delete_calls(client)) == len(_ROLLBACK_CLEANUP_TABLES)

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
