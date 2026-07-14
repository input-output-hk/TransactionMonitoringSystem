"""Cluster runs, their per-tx labels, cluster summaries/members, and the manual
tx verdict labels (malicious/benign)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .base import _RepoBase, _row_to_dict

_RUN_OUT_KEYS = [
    "run_id", "target", "feature_set", "eps", "min_samples", "metric",
    "n_points", "n_clusters", "n_noise", "silhouette", "origin", "created_at",
]
_RUN_INT_KEYS = ("min_samples", "n_points", "n_clusters", "n_noise")

# Columns written for a manual tx-label row (``updated_at`` defaults to now64 on
# insert so the newest write — including a deleted=1 tombstone — wins).
_TX_LABEL_COLUMNS = ["target", "tx_hash", "label", "source", "deleted", "note"]


class _ClusterMixin(_RepoBase):
    """Cluster-run persistence + reads, and the manual tx verdict labels."""

    def save_cluster_run(self, run: dict[str, Any]) -> None:
        cols = [
            "run_id",
            "target",
            "feature_set",
            "eps",
            "min_samples",
            "metric",
            "n_points",
            "n_clusters",
            "n_noise",
            "silhouette",
            "notes",
            "origin",
        ]
        # ``origin`` is a non-nullable Enum8; default missing values to the
        # column default so a caller that omits it can't insert NULL.
        row = [run.get("origin", "custom") if c == "origin" else run.get(c) for c in cols]
        self._insert("cluster_runs", cols, [row])

    def save_cluster_labels(self, run_id: str, labels: Sequence[tuple[str, int]]) -> None:
        data = [[run_id, tx_hash, int(cid)] for (tx_hash, cid) in labels]
        self._insert("cluster_labels", ["run_id", "tx_hash", "cluster_id"], data)

    _RUN_SELECT = (
        "SELECT run_id, target, feature_set, eps, min_samples, metric, "
        "n_points, n_clusters, n_noise, silhouette, origin, "
        "toString(created_at) AS created_at "
        "FROM {db}.cluster_runs FINAL {where}"
    )

    def list_runs(self, target: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE target = {t:String}" if target else ""
        sql = self._RUN_SELECT.format(db=self._db, where=where) + " ORDER BY created_at DESC"
        rows = self.client.query(
            sql, parameters={"t": target} if target else None
        ).result_rows
        return [self._run_row_to_dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        sql = self._RUN_SELECT.format(db=self._db, where="WHERE run_id = {r:String}") + " LIMIT 1"
        rows = self.client.query(sql, parameters={"r": run_id}).result_rows
        return self._run_row_to_dict(rows[0]) if rows else None

    @staticmethod
    def _run_row_to_dict(r: Sequence[Any]) -> dict[str, Any]:
        return _row_to_dict(
            _RUN_OUT_KEYS, r, int_keys=_RUN_INT_KEYS,
            float_keys=("eps",), nan_none_keys=("silhouette",),
        )

    def latest_cluster_run(
        self, target: str, feature_set: str, *, near: str | None = None
    ) -> dict[str, Any] | None:
        """Cluster run for a (target, feature_set): the most recent, or — with
        ``near`` (an anomaly run's ``created_at``) — the one closest in time, i.e.
        the sibling produced by the same pipeline pass (mirror of
        ``latest_anomaly_run(near=)``). The ``run_id`` tiebreaker keeps the pick
        deterministic when two runs share a ``created_at`` second."""
        if near is None:
            return self._latest_run(target, feature_set, system_only=False)
        where = "WHERE target = {t:String} AND feature_set = {f:String}"
        # Qualify created_at: _RUN_SELECT aliases `toString(created_at) AS
        # created_at`, and ClickHouse resolves bare ORDER BY identifiers to SELECT
        # aliases first — so the unqualified name is a String and dateDiff raises
        # ILLEGAL_TYPE_OF_ARGUMENT. The table-qualified form bypasses the alias.
        sql = self._RUN_SELECT.format(db=self._db, where=where) + (
            " ORDER BY abs(dateDiff('second', cluster_runs.created_at,"
            " parseDateTimeBestEffort({near:String}))) ASC,"
            " cluster_runs.created_at DESC, run_id DESC"
            " LIMIT 1"
        )
        rows = self.client.query(
            sql, parameters={"t": target, "f": feature_set, "near": near}
        ).result_rows
        return self._run_row_to_dict(rows[0]) if rows else None

    def latest_canonical_run(self, target: str, feature_set: str) -> dict[str, Any] | None:
        """Most recent *system-tuned* (canonical) cluster run for a (target,
        feature_set), or None when only user-supplied custom runs exist. This is
        the run the online model fits from, so a custom run never silently
        overrides the canonical model. Same deterministic tiebreaker as
        ``latest_cluster_run``."""
        return self._latest_run(target, feature_set, system_only=True)

    def _latest_run(
        self, target: str, feature_set: str, *, system_only: bool
    ) -> dict[str, Any] | None:
        """Newest run for a (target, feature_set) with the deterministic
        ``created_at DESC, run_id DESC`` tiebreaker; ``system_only`` restricts to
        canonical (system-tuned) runs."""
        where = "WHERE target = {t:String} AND feature_set = {f:String}"
        if system_only:
            where += " AND origin = 'system'"
        sql = (
            self._RUN_SELECT.format(db=self._db, where=where)
            + " ORDER BY created_at DESC, run_id DESC LIMIT 1"
        )
        rows = self.client.query(
            sql, parameters={"t": target, "f": feature_set}
        ).result_rows
        return self._run_row_to_dict(rows[0]) if rows else None

    def cluster_summary(self, run_id: str, target: str) -> list[dict[str, Any]]:
        # The count() alias is cluster_size, NOT size: transactions has a `size`
        # source column, and ClickHouse 26.x rejects an aggregate alias that
        # shadows a source column referenced by sibling aggregates (Code 184).
        rows = self.client.query(
            f"""
            SELECT
                cluster_id,
                count() AS cluster_size,
                round(avg(fees)) AS avg_fees,
                round(avg(total_output_lovelace)) AS avg_output_lovelace,
                round(avg(input_count), 2) AS avg_inputs,
                round(avg(output_count), 2) AS avg_outputs,
                round(avg(distinct_assets), 2) AS avg_assets
            FROM (
                SELECT tx_hash, cluster_id FROM {self._db}.cluster_labels FINAL
                WHERE run_id = {{r:String}}
            ) l
            INNER JOIN (
                SELECT tx_hash, fees, total_output_lovelace, input_count,
                       output_count, distinct_assets
                FROM {self._db}.transactions FINAL WHERE target = {{t:String}}
            ) t USING (tx_hash)
            GROUP BY cluster_id
            ORDER BY (cluster_id = -1), cluster_size DESC
            """,
            parameters={"r": run_id, "t": target},
        ).result_rows
        keys = [
            "cluster_id",
            "size",
            "avg_fees",
            "avg_output_lovelace",
            "avg_inputs",
            "avg_outputs",
            "avg_assets",
        ]
        return self._rows_to_dicts(keys, rows)

    def cluster_transactions(
        self, run_id: str, target: str, cluster_id: int, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        rows = self.client.query(
            f"""
            SELECT
                toString(t.tx_hash) AS tx_hash,
                toString(t.block_time) AS block_time,
                t.fees AS fees,
                t.total_output_lovelace AS total_output_lovelace,
                t.input_count AS input_count,
                t.output_count AS output_count,
                t.distinct_assets AS distinct_assets,
                t.redeemer_count AS redeemer_count
            FROM (
                SELECT tx_hash FROM {self._db}.cluster_labels FINAL
                WHERE run_id = {{r:String}} AND cluster_id = {{c:Int32}}
            ) l
            INNER JOIN (
                SELECT * FROM {self._db}.transactions FINAL WHERE target = {{t:String}}
            ) t USING (tx_hash)
            ORDER BY t.block_time DESC
            LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}
            """,
            parameters={
                "r": run_id,
                "c": cluster_id,
                "t": target,
                "lim": limit,
                "off": offset,
            },
        ).result_rows
        keys = [
            "tx_hash",
            "block_time",
            "fees",
            "total_output_lovelace",
            "input_count",
            "output_count",
            "distinct_assets",
            "redeemer_count",
        ]
        return self._rows_to_dicts(keys, rows)

    def run_tx_labels(self, run_id: str) -> dict[str, int]:
        rows = self.client.query(
            f"SELECT toString(tx_hash), cluster_id FROM {self._db}.cluster_labels FINAL "
            f"WHERE run_id = {{r:String}}",
            parameters={"r": run_id},
        ).result_rows
        return {str(tx): int(cid) for (tx, cid) in rows}

    def cluster_member_hashes(self, run_id: str, cluster_id: int) -> list[str]:
        rows = self.client.query(
            f"SELECT toString(tx_hash) FROM {self._db}.cluster_labels FINAL "
            f"WHERE run_id = {{r:String}} AND cluster_id = {{c:Int32}}",
            parameters={"r": run_id, "c": int(cluster_id)},
        ).result_rows
        return [str(r[0]) for r in rows]

    # --- Tx verdict labels (manual malicious/benign) ---------------------------

    def set_tx_labels(
        self,
        target: str,
        tx_hashes: Sequence[str],
        label: str,
        *,
        source: str = "cluster",
        note: str = "",
    ) -> int:
        """Write one ``tx_labels`` row per hash with the given verdict; returns the
        count written. Idempotent — re-labelling rewrites with a fresh ``updated_at``
        so the ReplacingMergeTree keeps the newest row. No-op on an empty list."""
        data = [[target, h, label, source, 0, note] for h in tx_hashes]
        self._insert("tx_labels", _TX_LABEL_COLUMNS, data)
        return len(data)

    def clear_tx_labels(self, target: str, tx_hashes: Sequence[str]) -> int:
        """Tombstone the explicit labels for these hashes (insert ``deleted=1``
        rows). Append-only, so the clear is immediately visible after FINAL merge.
        No-op on an empty list."""
        data = [[target, h, "benign", "cluster", 1, ""] for h in tx_hashes]
        self._insert("tx_labels", _TX_LABEL_COLUMNS, data)
        return len(data)

    def labels_for_target(self, target: str) -> dict[str, str]:
        """``{tx_hash: 'malicious'|'benign'}`` for all current (non-tombstoned)
        explicit labels of a target, regardless of source."""
        rows = self.client.query(
            f"SELECT toString(tx_hash), label FROM {self._db}.tx_labels FINAL "
            f"WHERE target = {{t:String}} AND deleted = 0",
            parameters={"t": target},
        ).result_rows
        return {str(tx): str(label) for (tx, label) in rows}

    def cluster_labeled_hashes(self, target: str) -> set[str]:
        """tx_hashes whose current label was applied at *cluster* granularity
        (``source = 'cluster'``) — the labels that propagate to unlabeled cluster
        members. A single-tx (``manual_tx``) label is excluded, so it colours only its
        own transaction. Feeds ``compute_verdicts``' ``propagating`` set."""
        rows = self.client.query(
            f"SELECT toString(tx_hash) FROM {self._db}.tx_labels FINAL "
            f"WHERE target = {{t:String}} AND deleted = 0 AND source = 'cluster'",
            parameters={"t": target},
        ).result_rows
        return {str(r[0]) for r in rows}
