"""Rollback purge: lightweight DELETEs must carry the projection setting.

ClickHouse >= 24.7 rejects lightweight DELETE on a projected table unless
lightweight_mutation_projection_mode is set, so a purge without it crash-loops
chain sync on the first real rollback (live verification:
scripts/verify_rollback_purge.py). These tests pin the setting on every
DELETE and the purge's structural behaviour against a mocked client.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.db import clickhouse
from app.db.clickhouse import (
    _LIGHTWEIGHT_DELETE_SETTINGS,
    _ROLLBACK_CLEANUP_TABLES,
    delete_clustering_rows,
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
        client.execute.side_effect = [[("aa" * 32,)]] + [None] * len(_ROLLBACK_CLEANUP_TABLES)
        delete_rolled_back_txs("preprod", 100)
        deletes = _delete_calls(client)
        assert len(deletes) == len(_ROLLBACK_CLEANUP_TABLES)
        for call in deletes:
            assert call.kwargs.get("settings") == _LIGHTWEIGHT_DELETE_SETTINGS

    def test_projection_mode_is_rebuild(self):
        # 'drop' would silently degrade reads on mutated parts; 'rebuild'
        # keeps the list-endpoint projection correct on surviving rows.
        assert _LIGHTWEIGHT_DELETE_SETTINGS["lightweight_mutation_projection_mode"] == "rebuild"


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
        client.execute.side_effect = [[("aa" * 32,)]] + [None] * len(_ROLLBACK_CLEANUP_TABLES)
        delete_rolled_back_txs("preprod", 100)
        deleted_tables = [
            c.args[0].split("DELETE FROM ")[1].split(" ")[0] for c in _delete_calls(client)
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
        assert not any("DELETE FROM transactions" in c.args[0] for c in _delete_calls(client))

        # Retry: the source still yields the hashes; every table is purged.
        client.execute.reset_mock()
        client.execute.side_effect = [hashes] + [None] * len(_ROLLBACK_CLEANUP_TABLES)
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


class TestClusteringPurge:
    """delete_clustering_rows: gated cross-database purge of the sidecar's
    verdicts so a rollback leaves no ghost contract_anomaly rows and re-confirmed
    txs get re-classified by the feed."""

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
        monkeypatch.setattr(settings, "CLUSTERING_DB", "tms_clustering")

    def test_noop_when_module_disabled(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CLUSTERING_ENABLED", False)
        delete_clustering_rows("preprod", ["aa" * 32])
        assert _delete_calls(client) == []

    def test_noop_on_empty_hashes(self, client):
        delete_clustering_rows("preprod", [])
        assert _delete_calls(client) == []

    def test_purges_both_sidecar_tables(self, client):
        delete_clustering_rows("preprod", ["aa" * 32])
        targets = [c.args[0].split("DELETE FROM ")[1].split(" ")[0] for c in _delete_calls(client)]
        assert targets == [
            "tms_clustering.tx_contract_anomaly",
            "tms_clustering.tx_classifications",
        ]

    def test_cross_db_deletes_carry_projection_mode(self, client):
        # Same hazard as the host purge: a projection on either sidecar table
        # makes an unsettinged lightweight DELETE throw on CH >= 24.7, which the
        # best-effort handler swallows -> a ghost verdict survives the rollback.
        delete_clustering_rows("preprod", ["aa" * 32])
        deletes = _delete_calls(client)
        assert len(deletes) == 2
        for call in deletes:
            assert call.kwargs.get("settings") == _LIGHTWEIGHT_DELETE_SETTINGS

    def test_classifications_purge_match_is_fixedstring_safe(self, client):
        # tx_classifications.tx_hash is FixedString(64); the host passes str
        # hashes, so the match must go through toString() (mirrors the read
        # path) or padding-sensitive comparison silently misses rows.
        delete_clustering_rows("preprod", ["aa" * 32])
        classif = next(c for c in _delete_calls(client) if "tx_classifications" in c.args[0])
        assert "toString(tx_hash)" in classif.args[0]

    def test_best_effort_swallows_missing_tables(self, client):
        # The sidecar may never have run; a rollback must not crash chain sync.
        client.execute.side_effect = RuntimeError("UNKNOWN_TABLE")
        delete_clustering_rows("preprod", ["aa" * 32])  # must not raise


class TestScoreRepurgeReach:
    """delete_score_rows deletes outright only what a scoring lane will score again.

    A rolled-back tx that re-confirmed is picked again only while its stamp lies
    ahead of the scoring checkpoint; a repurge replayed later must keep the
    scores written from that copy (see the live tier for the full sequence).
    """

    _GONE, _NEAR, _BEHIND = "aa" * 32, "bb" * 32, "cc" * 32

    def _repurge(self, client, reconfirmed):
        # The read of the re-confirmed copies, then the DELETEs.
        client.execute.side_effect = [reconfirmed] + [None] * len(reconfirmed + [None])
        clickhouse.delete_score_rows("preprod", [self._GONE, self._NEAR, self._BEHIND])
        return _delete_calls(client)

    def _stamps(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        reach = timedelta(seconds=clickhouse._scoring_reachable_seconds())
        # One live window inside and outside the reach.
        margin = timedelta(seconds=settings.ANALYSIS_LIVE_WINDOW_SECONDS)
        return now - reach + margin, now - reach - margin

    def test_only_a_copy_behind_the_checkpoint_keeps_its_newer_scores(self, client):
        near, behind = self._stamps()
        deletes = self._repurge(client, [(self._NEAR, near), (self._BEHIND, behind)])
        outright, bounded = deletes
        # Never re-confirmed, or still reached by a lane: every score row goes.
        assert outright.args[1]["hashes"] == [self._GONE, self._NEAR]
        assert "analyzed_at" not in outright.args[0]
        # Behind the checkpoint: only rows older than the copy now on chain.
        assert bounded.args[1]["hashes"] == [self._BEHIND]
        assert "analyzed_at < %(stamp)s" in bounded.args[0]
        assert bounded.args[1]["stamp"] == behind.replace(tzinfo=UTC)

    def test_every_repurge_delete_carries_projection_mode(self, client):
        near, behind = self._stamps()
        for call in self._repurge(client, [(self._NEAR, near), (self._BEHIND, behind)]):
            assert call.kwargs.get("settings") == _LIGHTWEIGHT_DELETE_SETTINGS

    def test_the_copy_read_bypasses_the_projection(self, client):
        # p_by_time_v2 can still serve the purged fork copy until its part merges.
        self._repurge(client, [])
        (read, *_) = client.execute.call_args_list
        assert "optimize_use_projections = 0" in read.args[0]

    def test_the_reach_is_inside_the_checkpoint_bound(self):
        # The checkpoint never passes now - window - settle.
        bound = settings.ANALYSIS_LIVE_WINDOW_SECONDS + settings.ANALYSIS_BACKLOG_SETTLE_SECONDS
        assert 0 < clickhouse._scoring_reachable_seconds() < bound
