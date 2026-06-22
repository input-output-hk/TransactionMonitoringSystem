"""Onboarded-contract registry rows and the hard-delete purge."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .base import _RepoBase, _row_to_dict

# Columns written for a contract row (``updated_at`` defaults to now64 on insert).
# The DB column ``present`` is exposed to callers/API as ``exists``.
CONTRACT_COLUMNS = [
    "target",
    "target_type",
    "label",
    "present",
    "is_script",
    "script_type",
    "balance_lovelace",
    "asset_count",
    "sample_tokens",
    "tx_count",
    "status",
    "requested_max_txs",
    "drift_score",
]
_CONTRACT_DEFAULTS: dict[str, Any] = {
    "label": "",
    "present": 0,
    "is_script": 0,
    "script_type": "",
    "balance_lovelace": 0,
    "asset_count": 0,
    "sample_tokens": "[]",
    "tx_count": 0,
    "status": "pending",
    "requested_max_txs": 0,
    # Reset to 0 whenever a writer omits it — notably the batch pipeline
    # (process_contract / re-analyze), which intentionally clears stale drift: a
    # fresh re-cluster supersedes the old frozen model. update_contract passes the
    # measured rate through explicitly.
    "drift_score": 0.0,
}

# Output column order for the public contract select (single source of truth shared
# by the SELECT projection and the row mapper) plus the int-coerced subset. The DB
# ``present`` column surfaces as ``exists``.
_CONTRACT_OUT_KEYS = [
    "target", "target_type", "label", "exists", "is_script", "script_type",
    "balance_lovelace", "asset_count", "sample_tokens", "status",
    "requested_max_txs", "updated_at", "tx_count", "drift_score",
]
_CONTRACT_INT_KEYS = (
    "exists", "is_script", "balance_lovelace", "asset_count", "requested_max_txs", "tx_count",
)
_CONTRACT_FLOAT_KEYS = ("drift_score",)

# Tables that carry a ``target`` column, purged directly when a contract is
# deleted. ``cluster_labels``/``anomaly_scores`` key on ``run_id`` instead and are
# purged separately (via their run tables) — see ``delete_contract``.
#
# ``contracts`` is purged LAST and on purpose: the per-table mutations aren't a
# single transaction (ClickHouse has none), so if one fails mid-purge the contract
# row must still exist for the delete endpoint to find it and re-run the (now
# mostly no-op) purge. Were ``contracts`` removed earlier, a later failure would
# strand orphan rows behind a 404.
_TARGET_KEYED_TABLES = (
    "transactions",
    "tx_utxos",
    "tx_utxo_assets",
    "ingest_cursor",
    "cluster_runs",
    "anomaly_runs",
    "jobs",
    "tx_labels",
    "cluster_models",
    "tx_classifications",
    "contracts",
)


class _ContractMixin(_RepoBase):
    """Contract registry persistence, reads, rename and hard-delete."""

    def save_contract(self, contract: dict[str, Any]) -> None:
        """Insert/refresh a contract row. ``exists`` maps to the ``present`` column.

        Missing fields fall back to ``_CONTRACT_DEFAULTS``; ``target`` and
        ``target_type`` are required.
        """
        row = {**_CONTRACT_DEFAULTS, **contract}
        if "exists" in contract:
            row["present"] = int(contract["exists"])
        self._insert("contracts", CONTRACT_COLUMNS, [[row[c] for c in CONTRACT_COLUMNS]])

    @staticmethod
    def _contract_row_to_dict(r: Sequence[Any]) -> dict[str, Any]:
        return _row_to_dict(
            _CONTRACT_OUT_KEYS, r, int_keys=_CONTRACT_INT_KEYS, float_keys=_CONTRACT_FLOAT_KEYS
        )

    # tx_count is the snapshot written by process_contract (the only writer of
    # transactions), so we read it directly instead of re-scanning the full
    # transactions table on every list call.
    _CONTRACT_SELECT = (
        "SELECT target, target_type, label, present, is_script, script_type, "
        "balance_lovelace, asset_count, sample_tokens, status, requested_max_txs, "
        "toString(updated_at) AS updated_at, tx_count, drift_score "
        "FROM {db}.contracts FINAL {where}"
    )

    def list_contracts(self) -> list[dict[str, Any]]:
        sql = self._CONTRACT_SELECT.format(db=self._db, where="") + " ORDER BY updated_at DESC"
        rows = self.client.query(sql).result_rows
        return [self._contract_row_to_dict(r) for r in rows]

    def get_contract(self, target: str) -> dict[str, Any] | None:
        sql = self._CONTRACT_SELECT.format(db=self._db, where="WHERE target = {t:String}") + " LIMIT 1"
        rows = self.client.query(sql, parameters={"t": target}).result_rows
        return self._contract_row_to_dict(rows[0]) if rows else None

    def update_contract_label(self, target: str, label: str) -> dict[str, Any] | None:
        """Set a contract's display ``label``, preserving all other columns.

        Re-inserts the row (``ReplacingMergeTree`` keeps the newest) and re-reads
        it so the returned ``updated_at`` reflects the new write; returns ``None``
        if the contract doesn't exist. This is a second writer to ``contracts``
        besides the job worker; a rename racing an in-flight job resolves to
        last-write-wins (acceptable — renames target finished contracts).
        """
        contract = self.get_contract(target)
        if contract is None:
            return None
        contract["label"] = label
        self.save_contract(contract)
        return self.get_contract(target)

    def delete_contract(self, target: str) -> dict[str, Any]:
        """Hard-purge a contract and ALL its data across every table.

        This is the one place that issues real row deletes (``ALTER … DELETE``)
        rather than the append-tombstone pattern used elsewhere: a full purge has
        to span 13 tables, so tombstones don't apply. ``mutations_sync = 2`` makes
        each mutation synchronous, so the call returns only once the data is gone.

        ``cluster_labels``/``anomaly_scores`` key on ``run_id`` (no ``target``
        column), so they're purged first — via a subquery against their run tables,
        which must still hold the rows at that point — before the run tables and
        the rest of the ``target``-keyed tables.
        """

        def _delete(table: str, where: str) -> None:
            self.client.command(
                f"ALTER TABLE {self._db}.{table} DELETE WHERE {where} SETTINGS mutations_sync = 2",
                parameters={"t": target},
            )

        _delete(
            "cluster_labels",
            f"run_id IN (SELECT run_id FROM {self._db}.cluster_runs WHERE target = {{t:String}})",
        )
        _delete(
            "anomaly_scores",
            f"run_id IN (SELECT run_id FROM {self._db}.anomaly_runs WHERE target = {{t:String}})",
        )
        for table in _TARGET_KEYED_TABLES:
            _delete(table, "target = {t:String}")
        return {"target": target}
