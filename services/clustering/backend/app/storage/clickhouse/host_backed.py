"""Host-backed repository: the engine's storage layer when it runs as the TMS
clustering sidecar (CHAIN_SOURCE=host_ch).

The engine's own state (clusters, anomalies, models, jobs, contracts, the
contract_anomaly projection) is still written to the engine database
(``CLICKHOUSE_DB=tms_clustering``) by the inherited mixins, unchanged. What
changes is where raw transaction/feature data is READ from: instead of the
engine's own ``transactions`` / ``tx_utxos`` tables (never populated in the
integrated deployment, so no data is duplicated), this repo reads the host
TMS's already-ingested chain data from ``HOST_CLICKHOUSE_DB`` (``tms_analytics``)
on the SAME ClickHouse server, via fully-qualified cross-database queries.

Three host-side gaps are bridged at read time so the engine's feature builders
consume exactly their expected column contract without any engine change:

- ``size`` comes from the additive host column ``transactions.tx_size_bytes``
  (forward-only; 0 for rows ingested before the column existed).
- ``distinct_assets`` is computed from the inputs/outputs ``assets`` JSON. Every
  transaction has outputs, so this LEFT JOIN never drops a row (unlike a join to
  ``tx_script_features``, which has no row for non-script txs).
- ``redeemer_count`` is a LEFT JOIN to ``tx_script_features`` with the unmatched
  (non-script) side coalesced to 0.

A watched contract is an address ``target``; its transactions are resolved from
``address_transactions`` and bounded to the most recent ``CLUSTERING_WINDOW_TXS``
(applied as an in-SQL subquery, never a multi-thousand-element array parameter)
so DBSCAN/IsolationForest and the O(n^2) silhouette stay bounded for a
high-volume mainnet contract. Writes to the engine's raw-tx tables and the
ingest cursor are no-ops (the host already ingested the chain; the sidecar never
downloads).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

from app.config import Settings
from app.models import AssetRecord, TxRecord, UtxoRecord

from . import ClickHouseRepo
from .ingest import TX_CONTEXT_KEYS


class HostBackedRepo(ClickHouseRepo):
    """ClickHouseRepo whose raw-tx/feature READS come from the host's
    ``tms_analytics`` while engine-owned state stays in ``tms_clustering``."""

    def __init__(self, settings: Settings | None = None, **kw: Any) -> None:
        super().__init__(settings, **kw)
        self._host_db = self._settings.host_clickhouse_db
        self._network = self._settings.cardano_network
        # 0 = unbounded (test/small contracts only); see CLUSTERING_WINDOW_TXS.
        self._window = int(self._settings.clustering_window_txs)

    # --- target -> windowed tx_hash subquery ----------------------------------

    def _hashes_expr(self) -> str:
        """An in-SQL subquery yielding the most recent ``CLUSTERING_WINDOW_TXS``
        distinct tx_hashes that touched the watched address ``{tgt}``, newest
        first by slot. Embedded directly so no large array crosses the HTTP
        boundary; the rolling window is applied HERE so the fit/classify
        populations and every read agree and stay bounded."""
        limit = "" if self._window <= 0 else "LIMIT {lim:UInt64}"
        return f"""(
            SELECT tx_hash FROM (
                SELECT tx_hash, max(slot) AS s
                FROM {self._host_db}.address_transactions
                WHERE network = {{net:String}} AND address = {{tgt:String}}
                GROUP BY tx_hash ORDER BY s DESC {limit}
            )
        )"""

    def _scope_params(self, target: str) -> dict[str, Any]:
        params: dict[str, Any] = {"net": self._network, "tgt": target}
        if self._window > 0:
            params["lim"] = self._window
        return params

    # --- engine-shaped transactions over host tables --------------------------

    def _tx_shaped(self, hashes_expr: str) -> str:
        """A derived table producing the engine's transaction column contract
        from host tables, for the tx_hashes in ``hashes_expr`` (either the
        windowed-target subquery or an explicit ``{hs:Array(String)}`` of new
        hashes). FINAL lives inside the per-table subqueries (ClickHouse 26
        forbids FINAL on a table directly inside a JOIN)."""
        h = self._host_db
        return f"""(
            SELECT
                toString(t.tx_hash) AS tx_hash,
                t.fee AS fees,
                t.tx_size_bytes AS size,
                t.input_count AS input_count,
                t.output_count AS output_count,
                ifNull(t.total_input_value, 0) AS total_input_lovelace,
                t.total_output_value AS total_output_lovelace,
                CAST(t.total_output_value AS Int64)
                    - CAST(ifNull(t.total_input_value, 0) AS Int64) AS net_lovelace,
                ifNull(da.distinct_assets, 0) AS distinct_assets,
                ifNull(sf.redeemer_count, 0) AS redeemer_count,
                t.timestamp AS block_time
            FROM (
                SELECT tx_hash, fee, tx_size_bytes, input_count, output_count,
                       total_input_value, total_output_value, timestamp
                FROM {h}.transactions FINAL
                WHERE network = {{net:String}} AND tx_hash IN {hashes_expr}
            ) t
            LEFT JOIN (
                SELECT tx_hash, toUInt32(max(redeemers_count)) AS redeemer_count
                FROM {h}.tx_script_features
                WHERE network = {{net:String}} AND tx_hash IN {hashes_expr}
                GROUP BY tx_hash
            ) sf USING (tx_hash)
            LEFT JOIN (
                SELECT tx_hash, toUInt32(uniqExact(unit)) AS distinct_assets
                FROM (
                    SELECT tx_hash, arrayJoin(JSONExtractKeys(assets)) AS unit
                    FROM {h}.transaction_outputs
                    WHERE network = {{net:String}} AND tx_hash IN {hashes_expr}
                      AND assets != '' AND assets != '{{}}'
                    UNION ALL
                    SELECT tx_hash, arrayJoin(JSONExtractKeys(assets)) AS unit
                    FROM {h}.transaction_inputs
                    WHERE network = {{net:String}} AND tx_hash IN {hashes_expr}
                      AND assets != '' AND assets != '{{}}'
                )
                GROUP BY tx_hash
            ) da USING (tx_hash)
        )"""

    def _windowed_tx(self) -> str:
        """The engine-shaped transactions derived table over the watched target's
        windowed tx_hashes: the single composition of _tx_shaped(_hashes_expr())
        that every windowed read (fetch_shape_features + the UI/anomaly joins) uses."""
        return self._tx_shaped(self._hashes_expr())

    def _addr_cooccurrence_sql(self, hashes_expr: str, *, order_by: str = "") -> str:
        """DISTINCT (tx_hash, address) over the host inputs+outputs for the txs in
        ``hashes_expr``. Shared by fetch_tx_addresses (windowed subquery, ORDER BY
        tx_hash) and fetch_addresses_for_txs (explicit hash array, no order)."""
        h = self._host_db
        return f"""
            SELECT DISTINCT toString(tx_hash) AS tx_hash, address FROM (
                SELECT tx_hash, address FROM {h}.transaction_outputs
                WHERE network = {{net:String}} AND tx_hash IN {hashes_expr} AND address != ''
                UNION DISTINCT
                SELECT tx_hash, address FROM {h}.transaction_inputs
                WHERE network = {{net:String}} AND tx_hash IN {hashes_expr} AND address != ''
            )
            {order_by}
        """

    _SHAPE_SELECT = """
        tx_hash, fees, size, input_count, output_count,
        total_input_lovelace, total_output_lovelace, net_lovelace,
        distinct_assets, redeemer_count,
        toHour(block_time) AS hour_of_day,
        toDayOfWeek(block_time) AS day_of_week
    """

    # --- feature reads (overrides) --------------------------------------------

    def fetch_shape_features(self, target: str) -> pd.DataFrame:
        return self.client.query_df(
            f"SELECT {self._SHAPE_SELECT} FROM {self._windowed_tx()} "
            "ORDER BY tx_hash",
            parameters=self._scope_params(target),
        )

    def fetch_shape_features_for(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame:
        # The caller (online classify) passes a bounded, chunked set of NEW
        # hashes, so an array parameter is safe here.
        if not tx_hashes:
            return pd.DataFrame()
        return self.client.query_df(
            f"SELECT {self._SHAPE_SELECT} FROM {self._tx_shaped('{hs:Array(String)}')} "
            "ORDER BY tx_hash",
            parameters={"net": self._network, "hs": list(tx_hashes)},
        )

    def fetch_tx_addresses(self, target: str) -> pd.DataFrame:
        return self.client.query_df(
            self._addr_cooccurrence_sql(self._hashes_expr(), order_by="ORDER BY tx_hash"),
            parameters=self._scope_params(target),
        )

    def fetch_addresses_for_txs(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame:
        if not tx_hashes:
            return pd.DataFrame(columns=["tx_hash", "address"])
        return self.client.query_df(
            self._addr_cooccurrence_sql("{hs:Array(String)}"),
            parameters={"net": self._network, "hs": list(tx_hashes)},
        )

    def count_transactions(self, target: str) -> int:
        rows = self.client.query(
            f"SELECT count() FROM {self._hashes_expr()}",
            parameters=self._scope_params(target),
        ).result_rows
        return int(rows[0][0]) if rows else 0

    def list_targets(self) -> list[dict[str, Any]]:
        # Raw-tx tables are empty in the integrated deployment; the watchlist is
        # the contracts registry.
        return [
            {"target": c["target"], "target_type": c.get("target_type", "address"),
             "tx_count": int(c.get("tx_count", 0) or 0)}
            for c in self.list_contracts()
        ]

    # --- transactions-joined reads that back the UI / online path -------------

    def latest_transactions(
        self, target: str, feature_set: str, *, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]:
        # `lim` is reserved for the window subquery; the result LIMIT uses a
        # distinct `rlim` so the two never clobber each other.
        params = self._scope_params(target)
        params.update({"f": feature_set, "rlim": limit, "off": offset})
        rows = self.client.query(
            f"""
            SELECT
                toString(t.tx_hash) AS tx_hash,
                toString(t.block_time) AS block_time, t.fees AS fees, t.size AS size,
                t.total_input_lovelace AS total_input_lovelace,
                t.total_output_lovelace AS total_output_lovelace,
                t.net_lovelace AS net_lovelace,
                t.input_count AS input_count, t.output_count AS output_count,
                t.distinct_assets AS distinct_assets, t.redeemer_count AS redeemer_count,
                c.cluster_id AS online_cluster_id, c.votes AS online_votes
            FROM {self._windowed_tx()} t
            LEFT JOIN (
                SELECT tx_hash, cluster_id, votes FROM {self._db}.tx_classifications FINAL
                WHERE target = {{tgt:String}} AND feature_set = {{f:String}}
            ) c ON t.tx_hash = c.tx_hash
            ORDER BY t.block_time DESC, t.tx_hash
            LIMIT {{rlim:UInt32}} OFFSET {{off:UInt32}}
            SETTINGS join_use_nulls = 1
            """,
            parameters=params,
        ).result_rows
        keys = ["tx_hash", *TX_CONTEXT_KEYS, "online_cluster_id", "online_votes"]
        return self._rows_to_dicts(keys, rows)

    def unclassified_tx_hashes(
        self, target: str, feature_set: str, *,
        run_id: str | None = None, model_id: str | None = None,
    ) -> list[str]:
        params = self._scope_params(target)
        params["f"] = feature_set
        model_clause = ""
        if model_id:
            model_clause = " AND model_id = {m:String}"
            params["m"] = model_id
        run_clause = ""
        if run_id:
            run_clause = (
                f" AND tx_hash NOT IN (SELECT tx_hash FROM {self._db}.cluster_labels FINAL "
                "WHERE run_id = {r:String})"
            )
            params["r"] = run_id
        sql = (
            f"SELECT tx_hash FROM {self._hashes_expr()} "
            f"WHERE tx_hash NOT IN (SELECT tx_hash FROM {self._db}.tx_classifications FINAL "
            "WHERE target = {tgt:String} AND feature_set = {f:String}" + model_clause + ")"
            f"{run_clause} ORDER BY tx_hash"
        )
        return [str(r[0]) for r in self.client.query(sql, parameters=params).result_rows]

    def top_anomalies(
        self, run_id: str, target: str, *, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]:
        params = self._scope_params(target)
        params.update({"r": run_id, "rlim": limit, "off": offset})
        rows = self.client.query(
            f"""
            SELECT
                toString(a.tx_hash) AS tx_hash,
                a.iso_score AS iso_score, a.lof_score AS lof_score,
                a.dbscan_noise AS dbscan_noise, a.consensus AS consensus,
                a.votes AS votes, a.score_rank AS score_rank,
                {_tx_context_aliased('t')},
                toHour(t.block_time) AS hour_of_day,
                toDayOfWeek(t.block_time) AS day_of_week
            FROM (
                SELECT tx_hash, iso_score, lof_score, dbscan_noise, consensus,
                       votes, score_rank
                FROM {self._db}.anomaly_scores FINAL WHERE run_id = {{r:String}}
            ) a
            INNER JOIN {self._windowed_tx()} t USING (tx_hash)
            ORDER BY a.score_rank
            LIMIT {{rlim:UInt32}} OFFSET {{off:UInt32}}
            """,
            parameters=params,
        ).result_rows
        keys = ["tx_hash", "iso_score", "lof_score", "dbscan_noise", "consensus",
                "votes", "score_rank", *TX_CONTEXT_KEYS, "hour_of_day", "day_of_week"]
        return self._rows_to_dicts(keys, rows, nan_none_keys=("iso_score", "lof_score"))

    def cluster_summary(self, run_id: str, target: str) -> list[dict[str, Any]]:
        params = self._scope_params(target)
        params["r"] = run_id
        # The count() alias is cluster_size, NOT size: _tx_shaped projects a
        # `size` column into the join input, and ClickHouse 26.x rejects an
        # aggregate alias that shadows a source column referenced by sibling
        # aggregates (Code 184).
        rows = self.client.query(
            f"""
            SELECT
                cluster_id, count() AS cluster_size,
                round(avg(fees)) AS avg_fees,
                round(avg(total_output_lovelace)) AS avg_output_lovelace,
                round(avg(input_count), 2) AS avg_inputs,
                round(avg(output_count), 2) AS avg_outputs,
                round(avg(distinct_assets), 2) AS avg_assets
            FROM (
                SELECT tx_hash, cluster_id FROM {self._db}.cluster_labels FINAL
                WHERE run_id = {{r:String}}
            ) l
            INNER JOIN {self._windowed_tx()} t USING (tx_hash)
            GROUP BY cluster_id
            ORDER BY (cluster_id = -1), cluster_size DESC
            """,
            parameters=params,
        ).result_rows
        keys = ["cluster_id", "size", "avg_fees", "avg_output_lovelace",
                "avg_inputs", "avg_outputs", "avg_assets"]
        return self._rows_to_dicts(keys, rows)

    def cluster_transactions(
        self, run_id: str, target: str, cluster_id: int, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        params = self._scope_params(target)
        params.update({"r": run_id, "c": cluster_id, "rlim": limit, "off": offset})
        rows = self.client.query(
            f"""
            SELECT
                toString(t.tx_hash) AS tx_hash, toString(t.block_time) AS block_time,
                t.fees AS fees, t.total_output_lovelace AS total_output_lovelace,
                t.input_count AS input_count, t.output_count AS output_count,
                t.distinct_assets AS distinct_assets, t.redeemer_count AS redeemer_count
            FROM (
                SELECT tx_hash FROM {self._db}.cluster_labels FINAL
                WHERE run_id = {{r:String}} AND cluster_id = {{c:Int32}}
            ) l
            INNER JOIN {self._windowed_tx()} t USING (tx_hash)
            ORDER BY t.block_time DESC
            LIMIT {{rlim:UInt32}} OFFSET {{off:UInt32}}
            """,
            parameters=params,
        ).result_rows
        keys = ["tx_hash", "block_time", "fees", "total_output_lovelace",
                "input_count", "output_count", "distinct_assets", "redeemer_count"]
        return self._rows_to_dicts(keys, rows)

    # --- writes the sidecar must not perform (no download, no duplication) ----

    def insert_transactions(self, rows: Sequence[TxRecord]) -> None:
        return None

    def insert_utxos(self, rows: Sequence[UtxoRecord]) -> None:
        return None

    def insert_assets(self, rows: Sequence[AssetRecord]) -> None:
        return None

    def get_cursor(self, target: str) -> dict[str, Any] | None:
        return None

    def upsert_cursor(self, target: str, target_type: str, **kw: Any) -> None:
        return None


def _tx_context_aliased(alias: str) -> str:
    """The TX_CONTEXT_SELECT projection over an already-shaped derived table
    (columns are the engine names), aliased ``{alias}`` and with block_time
    stringified to match TX_CONTEXT_SELECT."""
    parts = []
    for k in TX_CONTEXT_KEYS:
        if k == "block_time":
            parts.append(f"toString({alias}.block_time) AS block_time")
        else:
            parts.append(f"{alias}.{k} AS {k}")
    return ", ".join(parts)
