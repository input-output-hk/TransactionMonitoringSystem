"""Connection management and the shared row-mapping helpers used by every
repository mixin."""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import batched
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from app.config import Settings, get_settings


def connect(settings: Settings, *, database: str | None = None) -> Client:
    """Create a clickhouse_connect Client from settings. Single source for the
    connection parameters so a TLS flag / timeout change lands in one place; used
    by the repo's lazy client and the host_ch source.

    ``database`` overrides the default DB the client connects with. Pass
    ``"default"`` for first-run bootstrap: clickhouse_connect validates the
    connection's default database eagerly, so connecting with a not-yet-created
    DB raises UNKNOWN_DATABASE before any `CREATE DATABASE` can run."""
    # Server-side per-query settings. max_memory_usage only when > 0 (0 = leave
    # the server default). Both ceilings fail a query LOUDLY rather than
    # truncating it, so a recall-relevant fit never silently drops rows.
    query_settings: dict[str, Any] = {}
    if settings.clickhouse_max_memory_usage_bytes > 0:
        query_settings["max_memory_usage"] = settings.clickhouse_max_memory_usage_bytes
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_http_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=database or settings.clickhouse_db,
        connect_timeout=settings.clickhouse_connect_timeout_seconds,
        send_receive_timeout=settings.clickhouse_send_receive_timeout_seconds,
        settings=query_settings or None,
    )


def _row_to_dict(
    keys: list[str],
    row: Sequence[Any],
    *,
    int_keys: tuple[str, ...] = (),
    float_keys: tuple[str, ...] = (),
    nan_none_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Map a result row onto ``keys`` by position, coercing named columns:
    ``int_keys`` → int, ``float_keys`` → float, ``nan_none_keys`` → float then
    NaN → None (e.g. an undefined silhouette)."""
    d = dict(zip(keys, row, strict=True))
    for k in int_keys:
        d[k] = int(d[k])
    for k in float_keys:
        d[k] = float(d[k])
    for k in nan_none_keys:
        v = float(d[k])
        d[k] = None if math.isnan(v) else v
    return d


class _RepoBase:
    """Client lifecycle + batched-insert primitives shared by the mixins.

    Composed into ``ClickHouseRepo`` (see this package's ``__init__``); the mixins
    call ``self.client`` / ``self._insert`` / ``self._rows_to_dicts`` which resolve
    here via the MRO.
    """

    def __init__(self, settings: Settings | None = None, *, client: Client | None = None) -> None:
        self._settings = settings or get_settings()
        # Database name used to qualify every table reference. Tracks the same
        # setting the connection uses (below), so a non-default CLICKHOUSE_DB
        # doesn't connect to one DB and query another.
        self._db = self._settings.clickhouse_db
        self._client = client

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = connect(self._settings)
        return self._client

    def ping(self) -> bool:
        return self.client.query("SELECT 1").result_rows[0][0] == 1

    # --- Transaction-source hooks ------------------------------------------------
    # The five tx-joined reads (latest_transactions, unclassified_tx_hashes,
    # top_anomalies, cluster_summary, cluster_transactions) are written ONCE in
    # their mixins against these three hooks. The base repo serves them from the
    # engine's own `transactions` table; HostBackedRepo overrides ONLY the hooks
    # to source the identical column contract from the host's tms_analytics
    # tables, so the query shape and row mapping cannot drift between the repos.

    def _tx_relation(self) -> str:
        """A parenthesized derived table projecting the engine transaction column
        contract — ``toString(tx_hash) AS tx_hash, fees, size, input_count,
        output_count, total_input_lovelace, total_output_lovelace, net_lovelace
        (CAST subtraction), distinct_assets, redeemer_count, block_time`` — for
        the target parameter ``{tgt:String}`` (bound via ``_tx_scope_params``)."""
        return f"""(
            SELECT
                toString(tx_hash) AS tx_hash, fees, size, input_count, output_count,
                total_input_lovelace, total_output_lovelace,
                CAST(total_output_lovelace AS Int64) - CAST(total_input_lovelace AS Int64)
                    AS net_lovelace,
                distinct_assets, redeemer_count, block_time
            FROM {self._db}.transactions FINAL WHERE target = {{tgt:String}}
        )"""

    def _tx_hashes_relation(self) -> str:
        """A parenthesized derived table yielding just ``tx_hash`` for the target
        parameter ``{tgt:String}`` — the cheap sibling of ``_tx_relation`` for
        reads that only need membership."""
        return f"(SELECT tx_hash FROM {self._db}.transactions FINAL WHERE target = {{tgt:String}})"

    def _tx_scope_params(self, target: str) -> dict[str, Any]:
        """The query parameters ``_tx_relation``/``_tx_hashes_relation`` consume."""
        return {"tgt": target}

    # The schema the code expects of a live database. Init SQL creates it on a
    # fresh volume; `python -m app.cli migrate` brings an existing volume up to
    # date. Listed explicitly so the startup guard fails fast with a precise
    # message instead of a runtime "table/column not found".
    _EXPECTED_TABLES = (
        "transactions",
        "tx_utxos",
        "tx_utxo_assets",
        "ingest_cursor",
        "cluster_runs",
        "cluster_labels",
        "anomaly_runs",
        "anomaly_scores",
        "contracts",
        "jobs",
        "tx_labels",
        "cluster_models",
        "tx_classifications",
        "tx_contract_anomaly",
    )
    _EXPECTED_COLUMNS = (
        ("ingest_cursor", "cursor"),  # 006
        ("ingest_cursor", "source"),  # 006
        ("jobs", "kind"),  # 005
        ("cluster_runs", "origin"),  # 007 (retro-edited into 001 without an ALTER)
        ("anomaly_runs", "origin"),  # 007 (retro-edited into 002 without an ALTER)
        ("contracts", "drift_score"),  # 008
        ("tx_contract_anomaly", "published_at"),  # 009 (reconciliation version)
        ("contracts", "target_txs"),  # 010 (per-contract read window)
        ("contracts", "fit_coverage"),  # 011 (frozen-fit clusterability)
        ("contracts", "last_fit_at"),  # 011 (anti-flap re-fit cadence)
    )

    def missing_schema_objects(self) -> list[str]:
        """Tables/columns the code needs but the live DB lacks (empty = in sync).
        Drift happens when code is upgraded on an existing volume without running
        ``python -m app.cli migrate`` (init SQL only runs on fresh volumes)."""
        have_tables = {
            r[0]
            for r in self.client.query(
                "SELECT name FROM system.tables WHERE database = {db:String}",
                parameters={"db": self._db},
            ).result_rows
        }
        missing = [f"table {t}" for t in self._EXPECTED_TABLES if t not in have_tables]
        have_cols = {
            (r[0], r[1])
            for r in self.client.query(
                "SELECT table, name FROM system.columns WHERE database = {db:String}",
                parameters={"db": self._db},
            ).result_rows
        }
        missing += [
            f"column {t}.{c}"
            for t, c in self._EXPECTED_COLUMNS
            if t in have_tables and (t, c) not in have_cols
        ]
        return missing

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # Rows per INSERT for result-table writes that can carry a whole run at once
    # (cluster_labels / anomaly_scores on a 100k-tx contract): bounds the request
    # body well under ClickHouse's HTTP limits without measurable overhead.
    _INSERT_CHUNK = 10_000

    def _insert(self, table: str, columns: list[str], data: list[list[Any]]) -> None:
        """Batched insert into ``{self._db}.{table}``, chunked at ``_INSERT_CHUNK``
        rows; a no-op when ``data`` is empty."""
        for chunk in batched(data, self._INSERT_CHUNK, strict=False):
            self.client.insert(f"{self._db}.{table}", list(chunk), column_names=columns)

    def _insert_records(self, table: str, columns: list[str], records: Sequence[Any]) -> None:
        """Insert dataclass records, projecting each onto ``columns`` by attribute."""
        self._insert(table, columns, [[getattr(r, c) for c in columns] for r in records])

    @staticmethod
    def _rows_to_dicts(
        keys: list[str],
        rows: Sequence[Sequence[Any]],
        *,
        nan_none_keys: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Zip each result row against ``keys`` into a dict. ``nan_none_keys`` are
        coerced to float then NaN → None (e.g. ``iso_score`` where a detector didn't
        apply) — the plural counterpart of ``_row_to_dict``'s same option."""
        out = [dict(zip(keys, r, strict=True)) for r in rows]
        for d in out:
            for k in nan_none_keys:
                v = float(d[k])
                d[k] = None if math.isnan(v) else v
        return out
