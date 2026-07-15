"""Ingestion writes (transactions/utxos/assets), the resume cursor, the target
listing, and feature-extraction reads."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields
from typing import Any

import pandas as pd

from app.models import AssetRecord, TxRecord, UtxoRecord

from .base import _RepoBase

TX_COLUMNS = [f.name for f in fields(TxRecord)]
UTXO_COLUMNS = [f.name for f in fields(UtxoRecord)]
ASSET_COLUMNS = [f.name for f in fields(AssetRecord)]

# The canonical transaction shape-context columns, shared by the tx-joined reads
# that surface the full context (latest interactions, top anomalies). Those reads
# join the ``_tx_relation`` derived table (see base.py), which already projects
# every engine-named column including the derived ``net_lovelace``; the keys ARE
# the result-dict keys, in this order, and ``_tx_context_aliased`` renders the
# matching SELECT so a column can't drift between the projection and the mapping.
TX_CONTEXT_KEYS = [
    "block_time",
    "fees",
    "size",
    "total_input_lovelace",
    "total_output_lovelace",
    "net_lovelace",
    "input_count",
    "output_count",
    "distinct_assets",
    "redeemer_count",
]


def _tx_context_aliased(alias: str) -> str:
    """The ``TX_CONTEXT_KEYS`` projection over an already-shaped derived table
    (columns carry the engine names), aliased ``{alias}`` and with block_time
    stringified so the mapped value is a plain string, not a driver datetime."""
    parts = []
    for k in TX_CONTEXT_KEYS:
        if k == "block_time":
            parts.append(f"toString({alias}.block_time) AS block_time")
        else:
            parts.append(f"{alias}.{k} AS {k}")
    return ", ".join(parts)


class _IngestMixin(_RepoBase):
    """Fact-table inserts, ingest cursor, target listing and feature reads."""

    # --- Inserts ---------------------------------------------------------------

    def insert_transactions(self, rows: Sequence[TxRecord]) -> None:
        self._insert_records("transactions", TX_COLUMNS, rows)

    def insert_utxos(self, rows: Sequence[UtxoRecord]) -> None:
        self._insert_records("tx_utxos", UTXO_COLUMNS, rows)

    def insert_assets(self, rows: Sequence[AssetRecord]) -> None:
        self._insert_records("tx_utxo_assets", ASSET_COLUMNS, rows)

    # --- Ingest cursor ---------------------------------------------------------

    def get_cursor(self, target: str) -> dict[str, Any] | None:
        rows = self.client.query(
            f"SELECT target, target_type, cursor, source, last_page, last_tx_hash, "
            f"txs_seen, done "
            f"FROM {self._db}.ingest_cursor FINAL WHERE target = {{t:String}} LIMIT 1",
            parameters={"t": target},
        ).result_rows
        if not rows:
            return None
        keys = [
            "target",
            "target_type",
            "cursor",
            "source",
            "last_page",
            "last_tx_hash",
            "txs_seen",
            "done",
        ]
        row = self._rows_to_dicts(keys, rows)[0]
        # Legacy shim: rows written before 006_cursor.sql carry only the page
        # number. The migration backfills this same encoding; the shim covers a
        # row racing the backfill. Remove when last_page is dropped.
        if not row["cursor"] and int(row.get("last_page") or 0) > 0:
            row["cursor"] = f"page:{row['last_page']}"
        return row

    def upsert_cursor(
        self,
        target: str,
        target_type: str,
        *,
        cursor: str,
        last_tx_hash: str,
        txs_seen: int,
        done: bool,
    ) -> None:
        """Persist the source-owned resume cursor. ``source`` records which
        CHAIN_SOURCE produced it so a cursor is never replayed into a different
        provider; ``last_page`` is a dead legacy column (left at 0 on new rows)."""
        self._insert(
            "ingest_cursor",
            ["target", "target_type", "cursor", "source", "last_tx_hash", "txs_seen", "done"],
            [
                [
                    target,
                    target_type,
                    cursor,
                    self.settings.chain_source,
                    last_tx_hash,
                    txs_seen,
                    int(done),
                ]
            ],
        )

    # --- Targets ---------------------------------------------------------------

    def list_targets(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.client.query(
            f"SELECT target, any(target_type) AS target_type, "
            f"count(DISTINCT tx_hash) AS tx_count "
            f"FROM {self._db}.transactions GROUP BY target ORDER BY tx_count DESC "
            f"LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}",
            parameters={"lim": limit, "off": offset},
        ).result_rows
        return [{"target": t, "target_type": tt, "tx_count": int(c)} for (t, tt, c) in rows]

    def count_targets(self) -> int:
        """Distinct ingested targets: the full ``total`` for the paginated list."""
        rows = self.client.query(
            f"SELECT uniqExact(target) FROM {self._db}.transactions"
        ).result_rows
        return int(rows[0][0]) if rows else 0

    # --- Feature extraction ----------------------------------------------------

    # Shape feature projection — single source of truth shared by the
    # whole-target and by-hash fetches (and by app.features.shape, which expects
    # exactly these columns).
    _SHAPE_FEATURE_SELECT = """
        toString(tx_hash) AS tx_hash,
        fees,
        size,
        input_count,
        output_count,
        total_input_lovelace,
        total_output_lovelace,
        CAST(total_output_lovelace AS Int64) - CAST(total_input_lovelace AS Int64)
            AS net_lovelace,
        distinct_assets,
        redeemer_count,
        toHour(block_time) AS hour_of_day,
        toDayOfWeek(block_time) AS day_of_week
    """

    def fetch_shape_features(self, target: str) -> pd.DataFrame:
        """Per-tx numeric columns used to build the shape feature matrix."""
        return self.client.query_df(
            f"SELECT {self._SHAPE_FEATURE_SELECT} FROM {self._db}.transactions FINAL "
            "WHERE target = {t:String} ORDER BY tx_hash",
            parameters={"t": target},
        )

    def fetch_tx_addresses(self, target: str) -> pd.DataFrame:
        """(tx_hash, address, block_time) rows for the address co-occurrence
        features. ``block_time`` rides along so the graph down-sample keeps the
        most recent transactions rather than a hash-ordered slice. FINAL stays
        inside the per-table subqueries (ClickHouse 26 forbids FINAL on a table
        directly inside a JOIN)."""
        return self.client.query_df(
            f"""
            SELECT DISTINCT tx_hash, address, block_time FROM (
                SELECT toString(tx_hash) AS tx_hash, address
                FROM {self._db}.tx_utxos FINAL
                WHERE target = {{t:String}} AND address != ''
            ) u
            INNER JOIN (
                SELECT toString(tx_hash) AS tx_hash, block_time
                FROM {self._db}.transactions FINAL
                WHERE target = {{t:String}}
            ) t USING (tx_hash)
            ORDER BY tx_hash
            """,
            parameters={"t": target},
        )

    def fetch_addresses_for_txs(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame:
        if not tx_hashes:
            return pd.DataFrame(columns=["tx_hash", "address"])
        return self.client.query_df(
            f"""
            SELECT DISTINCT toString(tx_hash) AS tx_hash, address
            FROM {self._db}.tx_utxos FINAL
            WHERE target = {{t:String}} AND toString(tx_hash) IN {{hashes:Array(String)}}
              AND address != ''
            """,
            parameters={"t": target, "hashes": list(tx_hashes)},
        )

    def fetch_shape_features_for(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame:
        """Per-tx shape feature columns for a specific set of hashes (the new ones
        to score). Callers chunk to keep the ``IN`` array bounded."""
        if not tx_hashes:
            return pd.DataFrame()
        return self.client.query_df(
            f"SELECT {self._SHAPE_FEATURE_SELECT} FROM {self._db}.transactions FINAL "
            "WHERE target = {t:String} AND toString(tx_hash) IN {hs:Array(String)} "
            "ORDER BY tx_hash",
            parameters={"t": target, "hs": list(tx_hashes)},
        )

    def count_transactions(self, target: str) -> int:
        rows = self.client.query(
            f"SELECT count() FROM {self._db}.transactions FINAL WHERE target = {{t:String}}",
            parameters={"t": target},
        ).result_rows
        return int(rows[0][0]) if rows else 0
