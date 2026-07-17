"""Characterization tests for the hybrid (host + local history) repository.

A fake client records SQL + parameters, so the union shapes, the parameter
bindings and the factory routing are pinned without a real ClickHouse. The
UNION SQL itself is additionally validated against a live ClickHouse 26.1
before shipping (see the PR's verification notes): a mocked client cannot
catch server-side type-unification or alias-resolution errors.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd

from app.config import Settings
from app.models import TxRecord
from app.storage.clickhouse import ClickHouseRepo, select_repo_factory
from app.storage.clickhouse.host_backed import HostBackedRepo
from app.storage.clickhouse.hybrid import HybridHistoryRepo


class FakeClient:
    """Records every query's SQL + parameters; serves canned rows/frames."""

    def __init__(self, query_rows: list[tuple[Any, ...]] | None = None) -> None:
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.inserts: list[tuple[str, list[list[Any]], list[str]]] = []
        self._query_rows = query_rows or []

    def insert(
        self, table: str, data: list[list[Any]], column_names: list[str] | None = None
    ) -> None:
        self.inserts.append((table, data, column_names or []))

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        self.queries.append((sql, parameters))
        return SimpleNamespace(result_rows=self._query_rows)

    def query_df(self, sql: str, parameters: dict[str, Any] | None = None) -> pd.DataFrame:
        self.queries.append((sql, parameters))
        return pd.DataFrame()

    def close(self) -> None:
        pass


_SETTINGS = Settings(
    CHAIN_SOURCE="host_ch",
    HISTORY_SOURCE="blockfrost",
    CLICKHOUSE_DB="tms_clustering",
    HOST_CLICKHOUSE_DB="tms_analytics",
    CLUSTERING_WINDOW_TXS=100,
)


def _repo(query_rows: list[tuple[Any, ...]] | None = None) -> tuple[HybridHistoryRepo, FakeClient]:
    fake = FakeClient(query_rows)
    return HybridHistoryRepo(_SETTINGS, client=fake), fake


# --- factory routing ---------------------------------------------------------------


def test_factory_selects_hybrid_for_blockfrost_history() -> None:
    settings = Settings(
        CHAIN_SOURCE="host_ch", HISTORY_SOURCE="blockfrost", BLOCKFROST_PROJECT_ID="k"
    )
    assert select_repo_factory(settings) is HybridHistoryRepo


def test_factory_selects_host_backed_for_kupo_history() -> None:
    # Kupo history lands in the HOST tables, so plain host reads already see it.
    settings = Settings(CHAIN_SOURCE="host_ch", HISTORY_SOURCE="kupo")
    assert select_repo_factory(settings) is HostBackedRepo


def test_factory_plain_host_and_standalone_unaffected() -> None:
    assert select_repo_factory(Settings(CHAIN_SOURCE="host_ch")) is HostBackedRepo
    assert select_repo_factory(Settings(CHAIN_SOURCE="other")) is ClickHouseRepo


# --- union shapes -------------------------------------------------------------------


def test_hashes_expr_unions_local_and_host() -> None:
    repo, _fake = _repo()
    expr = repo._hashes_expr()
    assert "tms_analytics.address_transactions" in expr
    assert "tms_clustering.transactions FINAL" in expr
    assert "UNION ALL" in expr
    # Window over the union, aliases distinct from the source column.
    assert "ORDER BY s2 DESC LIMIT {lim:UInt64}" in expr
    assert "max(slot) AS s" in expr


def test_count_transactions_spans_the_union() -> None:
    repo, fake = _repo(query_rows=[(7,)])
    assert repo.count_transactions("addr1demo") == 7
    sql, params = fake.queries[0]
    assert "tms_analytics.address_transactions" in sql
    assert "tms_clustering.transactions FINAL" in sql
    assert params is not None and params["tgt"] == "addr1demo" and params["lim"] == 100


def test_tx_shaped_local_arm_excludes_host_hashes() -> None:
    repo, _fake = _repo()
    shaped = repo._tx_shaped("{hs:Array(String)}")
    # Host arm (the parent's derivation) plus the local arm with its guards.
    assert "tms_analytics.transactions FINAL" in shaped
    assert "tms_clustering.transactions FINAL" in shaped
    assert "toString(tx_hash) NOT IN" in shaped
    assert shaped.count("UNION ALL") >= 2  # distinct_assets union + arm union


def test_fetch_shape_features_for_binds_target() -> None:
    repo, fake = _repo()
    repo.fetch_shape_features_for("addr1demo", ["aa" * 32])
    sql, params = fake.queries[0]
    assert params is not None and params["tgt"] == "addr1demo"
    assert params["hs"] == ["aa" * 32]
    assert "NOT IN" in sql


def test_fetch_addresses_for_txs_unions_local_utxos_and_binds_target() -> None:
    repo, fake = _repo()
    repo.fetch_addresses_for_txs("addr1demo", ["aa" * 32])
    sql, params = fake.queries[0]
    assert "tms_clustering.tx_utxos FINAL" in sql
    assert "tms_analytics.transaction_outputs" in sql
    assert params is not None and params["tgt"] == "addr1demo"


def test_history_tx_count_reads_local_table_only() -> None:
    repo, fake = _repo(query_rows=[(3,)])
    assert repo.history_tx_count("addr1demo") == 3
    sql, _params = fake.queries[0]
    assert "tms_clustering.transactions FINAL" in sql
    assert "tms_analytics" not in sql


def test_host_backed_history_tx_count_is_zero() -> None:
    fake = FakeClient()
    repo = HostBackedRepo(Settings(CHAIN_SOURCE="host_ch"), client=fake)
    assert repo.history_tx_count("addr1demo") == 0
    assert fake.queries == []  # no query needed: no local rows by construction


def test_host_known_tx_hashes_queries_the_host_index_not_local() -> None:
    # The publish bound must query the HOST's address index (ignoring the
    # module's own local tables), so a host-known tx is never suppressed and a
    # host-unknown local-history tx never leaks into the projection.
    repo, fake = _repo(query_rows=[("aa" * 32,)])
    got = repo.host_known_tx_hashes("addr1demo", {"aa" * 32, "bb" * 32})
    assert got == {"aa" * 32}
    sql, params = fake.queries[0]
    assert "tms_analytics.address_transactions" in sql
    assert "tms_clustering" not in sql
    assert params is not None and params["tgt"] == "addr1demo"


def _tx_record() -> TxRecord:
    from datetime import UTC, datetime

    return TxRecord(
        target="t",
        target_type="address",
        tx_hash="aa" * 32,
        block_height=1,
        block_time=datetime(2023, 11, 14, tzinfo=UTC),
        slot=1,
        fees=200_000,
        deposit=0,
        size=300,
        valid_contract=1,
        input_count=1,
        output_count=1,
        total_input_lovelace=1_000_000,
        total_output_lovelace=900_000,
        distinct_input_addresses=1,
        distinct_output_addresses=1,
        distinct_assets=0,
        redeemer_count=0,
    )


def test_insert_and_cursor_remain_noops() -> None:
    # History writes go through a directly-constructed base repo; the request/
    # worker repo must never download (inherited no-ops stay no-ops).
    repo, fake = _repo()
    repo.insert_transactions([_tx_record()])
    repo.upsert_cursor("t", "address", cursor="page:1", last_tx_hash="", txs_seen=1, done=False)
    assert repo.get_cursor("t") is None
    assert fake.inserts == [] and fake.queries == []
