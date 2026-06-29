"""Characterization tests for the ClickHouse repository.

A fake client (injected via ``ClickHouseRepo(client=...)``) records inserts and
serves canned query rows, so the row-building and row-mapping behaviour is
pinned without a real ClickHouse.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.config import Settings
from app.models import AssetRecord, TxRecord, UtxoRecord
from app.storage.clickhouse import (
    ASSET_COLUMNS,
    CONTRACT_COLUMNS,
    JOB_COLUMNS,
    TX_COLUMNS,
    UTXO_COLUMNS,
    ClickHouseRepo,
    select_repo_factory,
)
from app.storage.clickhouse.host_backed import HostBackedRepo


class FakeClient:
    def __init__(self, query_rows: list[tuple[Any, ...]] | None = None) -> None:
        self.inserts: list[tuple[str, list[list[Any]], list[str]]] = []
        self.queries: list[str] = []
        self.commands: list[str] = []
        self._query_rows = query_rows or []

    def insert(
        self, table: str, data: list[list[Any]], column_names: list[str] | None = None
    ) -> None:
        self.inserts.append((table, data, column_names or []))

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        self.queries.append(sql)
        return SimpleNamespace(result_rows=self._query_rows)

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> None:
        self.commands.append(sql)

    def close(self) -> None:
        pass


def _repo(query_rows: list[tuple[Any, ...]] | None = None) -> tuple[ClickHouseRepo, FakeClient]:
    fake = FakeClient(query_rows)
    # Pin the database name so these table-qualification assertions ("tms.jobs"
    # etc.) test the qualification logic, not the value of the CLICKHOUSE_DB
    # default (which is "tms_clustering" to match the host integration).
    repo = ClickHouseRepo(Settings(CLICKHOUSE_DB="tms"), client=fake)
    return repo, fake


def test_select_repo_factory_host_ch_is_host_backed() -> None:
    """host_ch reads chain data from the host TMS tables, so both the worker and
    the per-request API repo must resolve to HostBackedRepo through this helper.
    A divergence is what made the co-spend graph endpoint 500 (the request repo
    queried the module's empty raw-tx tables)."""
    assert select_repo_factory(Settings(CHAIN_SOURCE="host_ch")) is HostBackedRepo


def test_select_repo_factory_downloading_adapter_is_base_repo() -> None:
    """A non-host_ch (downloading) adapter ingests into the module's own DB and
    uses the base ClickHouseRepo."""
    assert select_repo_factory(Settings(CHAIN_SOURCE="other")) is ClickHouseRepo


def test_host_backed_top_anomalies_includes_hour_and_day_of_week() -> None:
    """The host-backed top_anomalies must surface hour_of_day/day_of_week, which
    AnomalyCandidateOut requires; without them the /top endpoint 500'd with a
    ResponseValidationError. The score columns + every TX_CONTEXT_KEYS column +
    the two derived time fields are returned, in that order (zip is strict, so a
    row that doesn't match the key count would raise here)."""
    from app.storage.clickhouse.ingest import TX_CONTEXT_KEYS

    score_cols = ("a" * 64, 0.5, 1.2, 1, 0.9, 2, 1)  # tx_hash..score_rank
    ctx_cols = ("2026-06-02 03:38:22", 100, 0, 1000, 2000, 1000, 6, 11, 5, 2)
    derived = (3, 2)  # hour_of_day, day_of_week
    assert len(ctx_cols) == len(TX_CONTEXT_KEYS)  # ctx fixture stays in lockstep
    fake = FakeClient([(*score_cols, *ctx_cols, *derived)])
    repo = HostBackedRepo(Settings(CLICKHOUSE_DB="tms"), client=fake)

    rows = repo.top_anomalies("run-1", "addr", limit=10)

    # The mapped keys carry the two derived fields...
    assert rows[0]["hour_of_day"] == 3
    assert rows[0]["day_of_week"] == 2
    # ...and the SQL actually projects them (FakeClient ignores the query, so the
    # row arity alone can't catch dropping the columns from the SELECT only).
    assert "toHour(t.block_time)" in fake.queries[-1]
    assert "toDayOfWeek(t.block_time)" in fake.queries[-1]


def _tx() -> TxRecord:
    return TxRecord(
        target="addr",
        target_type="address",
        tx_hash="a" * 64,
        block_height=1,
        block_time=datetime(2024, 1, 1, tzinfo=UTC),
        slot=1,
        fees=200000,
        deposit=0,
        size=300,
        valid_contract=1,
        input_count=1,
        output_count=2,
        total_input_lovelace=1000,
        total_output_lovelace=900,
        distinct_input_addresses=1,
        distinct_output_addresses=2,
        distinct_assets=0,
        redeemer_count=1,
    )


# --- Inserts ---------------------------------------------------------------

def test_insert_transactions_builds_aligned_rows() -> None:
    repo, fake = _repo()
    repo.insert_transactions([_tx()])
    assert len(fake.inserts) == 1
    table, data, cols = fake.inserts[0]
    assert table == "tms.transactions"
    assert cols == TX_COLUMNS
    assert len(data) == 1 and len(data[0]) == len(TX_COLUMNS)
    assert data[0][TX_COLUMNS.index("tx_hash")] == "a" * 64
    assert data[0][TX_COLUMNS.index("fees")] == 200000


def test_insert_utxos_and_assets() -> None:
    repo, fake = _repo()
    repo.insert_utxos([UtxoRecord("addr", "a" * 64, "input", 0, "addrIn", 1000)])
    repo.insert_assets([AssetRecord("addr", "a" * 64, "output", 1, "policytok", 5)])
    assert fake.inserts[0][0] == "tms.tx_utxos"
    assert fake.inserts[0][2] == UTXO_COLUMNS
    assert fake.inserts[1][0] == "tms.tx_utxo_assets"
    assert fake.inserts[1][2] == ASSET_COLUMNS


def test_insert_empty_is_noop() -> None:
    repo, fake = _repo()
    repo.insert_transactions([])
    repo.insert_utxos([])
    repo.insert_assets([])
    assert fake.inserts == []


def test_upsert_cursor_converts_done_to_int() -> None:
    repo, fake = _repo()
    repo.upsert_cursor("addr", "address", cursor="page:2", last_tx_hash="bb", txs_seen=10, done=True)
    table, data, cols = fake.inserts[0]
    assert table == "tms.ingest_cursor"
    assert cols == ["target", "target_type", "cursor", "source", "last_tx_hash", "txs_seen", "done"]
    # source is the active CHAIN_SOURCE (host_ch) the repo's settings carry.
    assert data == [["addr", "address", "page:2", "host_ch", "bb", 10, 1]]


def test_save_cluster_labels() -> None:
    repo, fake = _repo()
    repo.save_cluster_labels("run1", [("aa", 0), ("bb", -1)])
    table, data, cols = fake.inserts[0]
    assert table == "tms.cluster_labels"
    assert cols == ["run_id", "tx_hash", "cluster_id"]
    assert data == [["run1", "aa", 0], ["run1", "bb", -1]]


def test_save_cluster_labels_empty_is_noop() -> None:
    repo, fake = _repo()
    repo.save_cluster_labels("run1", [])
    assert fake.inserts == []


def test_set_tx_labels_builds_rows() -> None:
    repo, fake = _repo()
    n = repo.set_tx_labels("addr", ["aa", "bb"], "malicious", note="x")
    assert n == 2
    table, data, cols = fake.inserts[0]
    assert table == "tms.tx_labels"
    assert cols == ["target", "tx_hash", "label", "source", "deleted", "note"]
    # updated_at omitted so the server stamps it; deleted=0 on a live label.
    assert data == [
        ["addr", "aa", "malicious", "cluster", 0, "x"],
        ["addr", "bb", "malicious", "cluster", 0, "x"],
    ]


def test_set_tx_labels_empty_is_noop() -> None:
    repo, fake = _repo()
    assert repo.set_tx_labels("addr", [], "benign") == 0
    assert fake.inserts == []


def test_clear_tx_labels_writes_tombstones() -> None:
    repo, fake = _repo()
    n = repo.clear_tx_labels("addr", ["aa"])
    assert n == 1
    table, data, _ = fake.inserts[0]
    assert table == "tms.tx_labels"
    assert data[0][4] == 1  # deleted tombstone


def test_clear_tx_labels_empty_is_noop() -> None:
    repo, fake = _repo()
    assert repo.clear_tx_labels("addr", []) == 0
    assert fake.inserts == []


def test_labels_for_target_maps_dict() -> None:
    repo, _ = _repo([("aa", "malicious"), ("bb", "benign")])
    assert repo.labels_for_target("addr") == {"aa": "malicious", "bb": "benign"}


def test_anomaly_votes_for_run_maps_dict() -> None:
    repo, _ = _repo([("aa", 3), ("bb", 0)])
    assert repo.anomaly_votes_for_run("an1") == {"aa": 3, "bb": 0}


def test_latest_anomaly_run_returns_id_or_none() -> None:
    repo, _ = _repo([("anomaly-shape-xyz",)])
    assert repo.latest_anomaly_run("addr", "shape") == "anomaly-shape-xyz"
    empty, _ = _repo([])
    assert empty.latest_anomaly_run("addr", "shape") is None


def test_cluster_member_hashes_maps_list() -> None:
    repo, _ = _repo([("aa",), ("bb",)])
    assert repo.cluster_member_hashes("run1", 0) == ["aa", "bb"]


def test_save_cluster_run_fills_missing_with_none() -> None:
    repo, fake = _repo()
    repo.save_cluster_run({"run_id": "run1", "target": "addr", "feature_set": "shape"})
    table, data, cols = fake.inserts[0]
    assert table == "tms.cluster_runs"
    assert data[0][cols.index("run_id")] == "run1"
    assert data[0][cols.index("eps")] is None  # not provided


# --- Query row-mapping -----------------------------------------------------

def test_get_cursor_maps_row() -> None:
    repo, _ = _repo([("addr", "address", "page:1", "host_ch", 1, "bb", 10, 0)])
    cur = repo.get_cursor("addr")
    assert cur == {
        "target": "addr",
        "target_type": "address",
        "cursor": "page:1",
        "source": "host_ch",
        "last_page": 1,
        "last_tx_hash": "bb",
        "txs_seen": 10,
        "done": 0,
    }


def test_get_cursor_synthesizes_legacy_page_cursor() -> None:
    # A pre-006 row (empty cursor, last_page set) still resumes correctly: the
    # read shim synthesizes the page-cursor encoding the migration backfills.
    repo, _ = _repo([("addr", "address", "", "host_ch", 3, "bb", 10, 0)])
    cur = repo.get_cursor("addr")
    assert cur is not None and cur["cursor"] == "page:3"


def test_get_cursor_none_when_empty() -> None:
    repo, _ = _repo([])
    assert repo.get_cursor("addr") is None


def test_cluster_summary_maps_rows() -> None:
    repo, _ = _repo([(0, 25, 170000.0, 1800000.0, 1.0, 1.0, 0.0)])
    rows = repo.cluster_summary("run1", "addr")
    assert rows == [
        {
            "cluster_id": 0,
            "size": 25,
            "avg_fees": 170000.0,
            "avg_output_lovelace": 1800000.0,
            "avg_inputs": 1.0,
            "avg_outputs": 1.0,
            "avg_assets": 0.0,
        }
    ]


def test_cluster_transactions_maps_rows() -> None:
    repo, _ = _repo([("aa", "2024-01-01 00:00:00", 170000, 1800000, 1, 1, 0, 1)])
    rows = repo.cluster_transactions("run1", "addr", 0, limit=10, offset=0)
    assert rows[0]["tx_hash"] == "aa"
    assert rows[0]["redeemer_count"] == 1


def test_save_anomaly_run_and_scores() -> None:
    repo, fake = _repo()
    repo.save_anomaly_run(
        {
            "run_id": "an1",
            "target": "addr",
            "feature_set": "shape",
            "methods": "isolation_forest,lof,dbscan",
            "n_points": 3,
            "n_flagged": 1,
            "eps": 1.5,
            "min_samples": 8,
            "top_quantile": 0.05,
        }
    )
    repo.save_anomaly_scores("an1", [("aa", 0.7, 1.2, 1, 0.9, 3, 1), ("bb", 0.1, 0.9, 0, 0.2, 0, 2)])
    run_table, run_data, run_cols = fake.inserts[0]
    score_table, score_data, score_cols = fake.inserts[1]
    assert run_table == "tms.anomaly_runs"
    assert score_table == "tms.anomaly_scores"
    # origin defaults to "custom" when the caller omits it (pipeline passes "system").
    assert run_cols[-1] == "origin"
    assert run_data[0][-1] == "custom"
    assert score_cols[0] == "run_id" and score_cols[-1] == "score_rank"
    assert score_data[0] == ["an1", "aa", 0.7, 1.2, 1, 0.9, 3, 1]


def test_top_anomalies_maps_and_nan_iso() -> None:
    nan = float("nan")
    # Columns: rank, tx, consensus, votes, iso, lof, dbscan, block_time, fees,
    # size, total_input, total_output, net, in, out, assets, redeemers, hour, dow.
    repo, _ = _repo(
        [
            (1, "aa", 0.95, 3, nan, 1.8, 1, "2024-01-01 00:00:00",
             200000, 500, 2000000, 1800000, -200000, 8, 2, 1, 1, 0, 1),
            (2, "bb", 0.40, 1, 0.5, 0.9, 0, "2024-01-02 00:00:00",
             300000, 600, 1000000, 900000, -100000, 2, 2, 0, 0, 12, 2),
        ]
    )
    rows = repo.top_anomalies("an1", "addr", limit=10)
    assert rows[0]["score_rank"] == 1
    assert rows[0]["tx_hash"] == "aa"
    assert rows[0]["votes"] == 3
    assert rows[0]["iso_score"] is None  # NaN -> None
    assert rows[1]["iso_score"] == 0.5


def test_latest_transactions_maps_rows_and_null_online() -> None:
    # Columns: tx, block_time, fees, size, total_input, total_output, net, in, out,
    # assets, redeemer, online_cluster_id, online_votes (the last two NULL when the
    # LEFT JOIN to tx_classifications misses — join_use_nulls = 1).
    repo, _ = _repo(
        [
            ("aa", "2024-01-02 00:00:00", 200000, 500, 2000000, 1800000, -200000,
             1, 2, 1, 1, 0, 2),
            ("bb", "2024-01-01 00:00:00", 300000, 600, 1000000, 900000, -100000,
             1, 2, 0, 1, None, None),
        ]
    )
    rows = repo.latest_transactions("addr", "shape", limit=10)
    assert rows[0]["tx_hash"] == "aa" and rows[0]["net_lovelace"] == -200000
    assert rows[0]["online_cluster_id"] == 0 and rows[0]["online_votes"] == 2
    # Unmatched online side comes back as NULL, not 0 — distinguishes "not scored".
    assert rows[1]["online_cluster_id"] is None and rows[1]["online_votes"] is None
    assert rows[1]["redeemer_count"] == 1


def test_get_anomaly_run_maps_row() -> None:
    repo, _ = _repo(
        [("an1", "addr", "shape", "isolation_forest,lof,dbscan", 5000, 42, 1.56, 32, 0.05,
          "custom", "2024-01-01 00:00:00")]
    )
    run = repo.get_anomaly_run("an1")
    assert run is not None
    assert run["n_flagged"] == 42
    assert run["methods"] == "isolation_forest,lof,dbscan"
    assert run["min_samples"] == 32
    assert run["origin"] == "custom"


def test_get_run_nan_silhouette_becomes_none() -> None:
    repo, _ = _repo(
        [("run1", "addr", "shape", 1.5, 5, "euclidean", 50, 2, 0, math.nan, "custom",
          "2024-01-01 00:00:00")]
    )
    run = repo.get_run("run1")
    assert run is not None
    assert run["silhouette"] is None
    assert run["n_clusters"] == 2
    assert run["origin"] == "custom"


# --- Contracts -------------------------------------------------------------

def test_save_contract_maps_exists_and_fills_defaults() -> None:
    repo, fake = _repo()
    repo.save_contract(
        {
            "target": "addr1x",
            "target_type": "address",
            "exists": 1,
            "is_script": 1,
            "balance_lovelace": 5_000_000,
            "status": "done",
            "tx_count": 10,
        }
    )
    table, data, cols = fake.inserts[0]
    assert table == "tms.contracts"
    assert cols == CONTRACT_COLUMNS
    row = data[0]
    assert row[cols.index("present")] == 1  # exists -> present
    assert row[cols.index("balance_lovelace")] == 5_000_000
    assert row[cols.index("status")] == "done"
    assert row[cols.index("tx_count")] == 10
    assert row[cols.index("script_type")] == ""  # default
    assert row[cols.index("sample_tokens")] == "[]"  # default
    assert "updated_at" not in cols  # defaulted server-side


def test_list_contracts_maps_rows() -> None:
    repo, _ = _repo(
        [
            (
                "addr1x", "address", "my label", 1, 1, "plutusV2", 5_000_000, 2,
                '[{"name":"A"}]', "done", 500, "2026-06-05 10:00:00.000", 10, 0.42,
            )
        ]
    )
    rows = repo.list_contracts()
    assert rows[0]["exists"] == 1  # from present column
    assert rows[0]["script_type"] == "plutusV2"
    assert rows[0]["balance_lovelace"] == 5_000_000
    assert rows[0]["tx_count"] == 10  # live count from join
    assert rows[0]["status"] == "done"
    assert rows[0]["drift_score"] == 0.42


# --- Jobs ------------------------------------------------------------------

def test_create_job_inserts_queued_row() -> None:
    repo, fake = _repo()
    repo.create_job("job-1", "addr1x", "address", 100, 0)
    table, data, _cols = fake.inserts[0]
    assert table == "tms.jobs"
    assert data[0] == ["job-1", "addr1x", "address", 100, 0, "onboard", "queued"]


def test_update_job_preserves_created_at_omits_updated_at() -> None:
    created = datetime(2026, 6, 5, 10, tzinfo=UTC)
    # FakeClient returns this row for the _job_row read (JOB_COLUMNS order).
    repo, fake = _repo(
        [("job-1", "addr1x", "address", 100, 0, "onboard", "queued", "", 0, "", created)]
    )
    repo.update_job("job-1", status="downloading", stage_detail="page 1", txs_done=100)
    table, data, cols = fake.inserts[0]
    assert table == "tms.jobs"
    assert cols == JOB_COLUMNS
    assert "updated_at" not in cols  # server stamps a fresh now64
    row = data[0]
    assert row[cols.index("status")] == "downloading"
    assert row[cols.index("stage_detail")] == "page 1"
    assert row[cols.index("txs_done")] == 100
    assert row[cols.index("created_at")] == created  # preserved


def test_get_job_maps_row() -> None:
    repo, _ = _repo(
        [
            (
                "job-1", "addr1x", "address", 100, 0, "onboard", "done", "5000 txs", 5000, "",
                "2026-06-05 10:00:00.000", "2026-06-05 10:05:00.000",
            )
        ]
    )
    job = repo.get_job("job-1")
    assert job is not None
    assert job["status"] == "done"
    assert job["txs_done"] == 5000
    assert job["max_txs"] == 100
    assert job["created_at"] == "2026-06-05 10:00:00.000"


def test_latest_cluster_run_near_orders_by_the_real_datetime_column() -> None:
    """_RUN_SELECT aliases `toString(created_at) AS created_at`; a bare ORDER BY
    identifier resolves to that String alias and dateDiff 500s live
    (ILLEGAL_TYPE_OF_ARGUMENT — fakes never execute SQL, so this pins the query
    text). The near= ordering must reference the table-qualified column."""
    repo, fake = _repo([])
    repo.latest_cluster_run("addr", "shape", near="2026-01-01 00:00:00")
    sql = fake.queries[-1]
    assert "dateDiff('second', cluster_runs.created_at" in sql
    assert "parseDateTimeBestEffort({near:String})" in sql


def test_delete_contract_purges_contract_anomaly_projection() -> None:
    """Deleting a watched contract must also drop the host-visible projection
    (tx_contract_anomaly), or stale Contract Anomaly rows for a no-longer-watched
    target keep surfacing in the host's /api/analysis/results."""
    repo, fake = _repo()
    repo.delete_contract("addr1")
    deleted = [
        c.split("ALTER TABLE ")[1].split(" DELETE")[0]
        for c in fake.commands
        if "ALTER TABLE" in c and " DELETE" in c
    ]
    assert "tms.tx_contract_anomaly" in deleted
    # contracts is purged LAST so a mid-purge failure still leaves the row for the
    # delete endpoint to find and re-run the (now mostly no-op) purge.
    assert deleted[-1] == "tms.contracts"
