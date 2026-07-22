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
from itertools import batched
from typing import Any

import pandas as pd

from app.config import Settings
from app.models import AssetRecord, TxRecord, UtxoRecord

from . import ClickHouseRepo


class HostBackedRepo(ClickHouseRepo):
    """ClickHouseRepo whose raw-tx/feature READS come from the host's
    ``tms_analytics`` while engine-owned state stays in ``tms_clustering``."""

    def __init__(self, settings: Settings | None = None, **kw: Any) -> None:
        super().__init__(settings, **kw)
        self._host_db = self._settings.host_clickhouse_db
        self._network = self._settings.cardano_network
        # The window CEILING (and the "is windowing on at all" switch): 0 =
        # unbounded (test/small contracts only). A contract's ACTUAL window is
        # per-contract (_window_for), clamped to this; an unset contract uses the
        # ceiling itself, so this stays the effective window for them. See
        # CLUSTERING_WINDOW_TXS / Settings.effective_window_txs.
        self._window = int(self._settings.clustering_window_txs)

    # --- target -> windowed tx_hash subquery ----------------------------------

    def _host_addr_index(self) -> str:
        """FROM/WHERE core of "the host's address-index rows for the watched
        target": the one scoping predicate the window reads, the hybrid's host
        arms and the publish bound all share, kept in one place so the sites
        cannot drift apart. Callers prepend their own projection and may append
        further AND clauses; every site binds ``{net}``/``{tgt}``."""
        return (
            f"{self._host_db}.address_transactions "
            "WHERE network = {net:String} AND address = {tgt:String}"
        )

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
                FROM {self._host_addr_index()}
                GROUP BY tx_hash ORDER BY s DESC {limit}
            )
        )"""

    def _target_requested_max_txs(self, target: str) -> int:
        """This contract's onboarded "latest N to cluster on" (0 = none set), as
        a single-column point-read on the small registry table. Split out from
        the full ``get_contract`` so the per-read window resolution does not pay
        the 13-column projection + row-map on every windowed query."""
        rows = self.client.query(
            f"SELECT requested_max_txs FROM {self._db}.contracts FINAL "
            "WHERE target = {t:String} LIMIT 1",
            parameters={"t": target},
        ).result_rows
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    def _window_for(self, target: str) -> int:
        """The rolling-window size for THIS contract: its onboarded "latest N to
        cluster on" (``requested_max_txs``), clamped to the recall floor and the
        ceiling by ``Settings.effective_window_txs``. Bound as the ``lim`` param
        of every windowed read (fit, count, the UI/anomaly joins), so the fit
        population, the card's tx_count and every read agree on the same N.

        Resolved per read: N is operator-editable, so a cached value would score
        a contract on a stale window after a change; the lookup is one indexed
        FINAL point-read on the small registry table. An unknown target (no row:
        tests, ad-hoc targets) resolves to the ceiling via
        effective_window_txs(0), preserving the pre-per-contract behavior."""
        return self._settings.effective_window_txs(self._target_requested_max_txs(target))

    def _scope_params(self, target: str) -> dict[str, Any]:
        params: dict[str, Any] = {"net": self._network, "tgt": target}
        # The LIMIT clause is present in the query string iff windowing is on at
        # all (self._window > 0; see _hashes_expr); when it is, bind the
        # per-contract window. effective_window_txs returns > 0 whenever the
        # ceiling is > 0, so lim is always bound when the clause needs it.
        if self._window > 0:
            params["lim"] = self._window_for(target)
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

    # --- tx-source hooks (see base._tx_relation) -------------------------------
    # The five tx-joined reads (latest_transactions, unclassified_tx_hashes,
    # top_anomalies, cluster_summary, cluster_transactions) live ONCE in the
    # base mixins; this repo redirects only their transaction source to the
    # host-shaped, windowed derived tables above. This is the single bridge
    # point between the host's column vocabulary and the engine's.

    def _tx_relation(self) -> str:
        return self._windowed_tx()

    def _tx_hashes_relation(self) -> str:
        return self._hashes_expr()

    def _tx_scope_params(self, target: str) -> dict[str, Any]:
        return self._scope_params(target)

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
            f"SELECT {self._SHAPE_SELECT} FROM {self._windowed_tx()} ORDER BY tx_hash",
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
        # block_time rides along (joined from the windowed derived table) so the
        # graph down-sample keeps the most recent transactions rather than a
        # hash-ordered slice.
        return self.client.query_df(
            f"""
            SELECT tx_hash, address, block_time
            FROM ({self._addr_cooccurrence_sql(self._hashes_expr())}) a
            INNER JOIN {self._windowed_tx()} t USING (tx_hash)
            ORDER BY tx_hash
            """,
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

    def list_targets(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        # Raw-tx tables are empty in the integrated deployment; the watchlist is
        # the contracts registry, so the page comes from it too.
        return [
            {
                "target": c["target"],
                "target_type": c.get("target_type", "address"),
                "tx_count": int(c.get("tx_count", 0) or 0),
            }
            for c in self.list_contracts(limit=limit, offset=offset)
        ]

    def count_targets(self) -> int:
        # Must count the same source list_targets pages over: the contracts
        # registry, not the (empty) module-local transactions table.
        return self.count_contracts()

    def history_tx_count(self, target: str) -> int:
        # Pure host mode has no local rows by construction (the raw tables stay
        # empty); the hybrid subclass overrides this with the local count.
        return 0

    # Membership-check chunk size: bounds each IN(...) array the same way the
    # online classify path bounds its scoring chunks (_CLASSIFY_BATCH rationale).
    _HOST_MEMBERSHIP_CHUNK = 1000

    def host_known_tx_hashes(self, target: str, tx_hashes: set[str]) -> set[str]:
        """The subset of ``tx_hashes`` present in the HOST's address index for
        the watched target. Exact regardless of what the engine's local tables
        contain, which is what makes it safe as the publish bound: a host-known
        tx is never suppressed (recall first), a host-unknown one never leaks
        into the host-facing projection. Chunked so the IN array stays bounded."""
        found: set[str] = set()
        for chunk in batched(sorted(tx_hashes), self._HOST_MEMBERSHIP_CHUNK, strict=False):
            rows = self.client.query(
                f"SELECT DISTINCT toString(tx_hash) FROM {self._host_addr_index()} "
                "AND toString(tx_hash) IN {hs:Array(String)}",
                parameters={"net": self._network, "tgt": target, "hs": list(chunk)},
            ).result_rows
            found.update(str(r[0]) for r in rows)
        return found

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
