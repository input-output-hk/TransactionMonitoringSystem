"""Persisted cluster models (fit artifacts) + per-tx online classifications."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .base import _RepoBase, _row_to_dict
from .ingest import TX_CONTEXT_KEYS, TX_CONTEXT_SELECT

# Columns for a persisted cluster model and a per-tx online classification
# (``created_at``/``scored_at`` default to now64 on insert; newest wins).
_MODEL_COLUMNS = [
    "model_id", "target", "feature_set", "run_id", "schema_version",
    "n_clusters", "n_train", "eps", "min_samples", "blob",
]
_CLASSIFICATION_COLUMNS = [
    "target", "tx_hash", "feature_set", "model_id", "cluster_id",
    "iso_score", "lof_score", "votes", "consensus", "verdict",
]


class _ModelMixin(_RepoBase):
    """Cluster-model persistence and the online classification reads/writes."""

    def save_cluster_model(self, model: dict[str, Any]) -> None:
        # All columns are required; KeyError at the boundary beats a silent NULL
        # insert into a non-nullable column.
        self._insert("cluster_models", _MODEL_COLUMNS, [[model[c] for c in _MODEL_COLUMNS]])

    def latest_cluster_model(self, target: str, feature_set: str) -> dict[str, Any] | None:
        """Newest model for a (target, feature_set), including its serialized blob."""
        rows = self.client.query(
            f"SELECT {', '.join(_MODEL_COLUMNS)} FROM {self._db}.cluster_models FINAL "
            "WHERE target = {t:String} AND feature_set = {f:String} "
            "ORDER BY created_at DESC LIMIT 1",
            parameters={"t": target, "f": feature_set},
        ).result_rows
        if not rows:
            return None
        return _row_to_dict(
            _MODEL_COLUMNS, rows[0],
            int_keys=("schema_version", "n_clusters", "n_train", "min_samples"),
            float_keys=("eps",),
        )

    def online_noise_rate(
        self, target: str, feature_set: str, model_id: str, *, window: int = 500
    ) -> tuple[float, int]:
        """Trailing "online-noise rate" for a model: the fraction of its most
        recently scored ``window`` txs that fell outside every frozen cluster
        (``cluster_id == -1``). This is the drift sensor — high when recent traffic
        no longer fits the frozen clusters (new behaviour or distribution drift),
        signalling the model should be re-clustered.

        Returns ``(rate, n)`` where ``n`` is the window actually sampled (< window
        when the model has scored fewer txs); ``(0.0, 0)`` when nothing is scored.
        ``FINAL`` collapses the ReplacingMergeTree to the latest row per tx; the
        trailing window keeps a one-off historical burst from dominating and lets
        the signal recover as fresh traffic is classified. Like
        ``unclassified_tx_hashes`` this is an O(history) scan + sort per call — fine
        for the manual classify button; the streaming phase should bound it with a
        scored watermark (see docs/online-classification-design.md)."""
        rows = self.client.query(
            "SELECT avg(cluster_id = -1), count() FROM ("
            f"  SELECT cluster_id FROM {self._db}.tx_classifications FINAL "
            "   WHERE target = {t:String} AND feature_set = {f:String} "
            "     AND model_id = {m:String} "
            "   ORDER BY scored_at DESC LIMIT {w:UInt32}"
            ")",
            parameters={"t": target, "f": feature_set, "m": model_id, "w": window},
        ).result_rows
        if not rows or rows[0][1] == 0:
            return 0.0, 0
        return float(rows[0][0]), int(rows[0][1])

    def save_tx_classifications(self, rows: Sequence[Sequence[Any]]) -> int:
        """Insert per-tx classification rows (columns in `_CLASSIFICATION_COLUMNS`
        order). No-op on empty; returns the count written."""
        data = [list(r) for r in rows]
        if not data:
            return 0
        self._insert("tx_classifications", _CLASSIFICATION_COLUMNS, data)
        return len(data)

    def latest_transactions(
        self, target: str, feature_set: str, *, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]:
        """The latest ``limit`` transactions for a target (newest ``block_time`` first),
        regardless of whether they've been classified — the recency-first feed behind the
        Latest tab. Each row carries its shape context plus the CURRENT model's online
        cluster assignment (``online_cluster_id``/``online_votes``) when one exists.

        ``FINAL`` collapses the ``tx_classifications`` ReplacingMergeTree to the
        latest-scored row per tx, so the join surfaces the current model's classification
        without filtering on ``model_id``. ``SETTINGS join_use_nulls = 1`` makes the
        unmatched side come back as ``NULL`` rather than 0 — without it a brand-new tx
        would be indistinguishable from one assigned to ``cluster 0``. The service uses
        ``online_cluster_id is None`` to mean "not online-scored" and layers batch
        membership on top. Batch-classified txs (no online row) come back with NULL online
        signals; the service still resolves their verdict from the cluster run."""
        rows = self.client.query(
            f"""
            SELECT
                toString(t.tx_hash) AS tx_hash,
                {TX_CONTEXT_SELECT},
                c.cluster_id AS online_cluster_id,
                c.votes AS online_votes
            FROM (SELECT * FROM {self._db}.transactions FINAL WHERE target = {{t:String}}) t
            LEFT JOIN (
                SELECT tx_hash, cluster_id, votes FROM {self._db}.tx_classifications FINAL
                WHERE target = {{t:String}} AND feature_set = {{f:String}}
            ) c ON t.tx_hash = c.tx_hash
            ORDER BY t.block_time DESC, t.tx_hash
            LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}
            SETTINGS join_use_nulls = 1
            """,
            parameters={"t": target, "f": feature_set, "lim": limit, "off": offset},
        ).result_rows
        keys = ["tx_hash", *TX_CONTEXT_KEYS, "online_cluster_id", "online_votes"]
        return self._rows_to_dicts(keys, rows)

    def unclassified_tx_hashes(
        self,
        target: str,
        feature_set: str,
        *,
        run_id: str | None = None,
        model_id: str | None = None,
    ) -> list[str]:
        """tx_hashes for a target that are neither already classified online by the
        CURRENT model nor members of the model's source cluster run (which
        classified them in batch).

        ``model_id`` scopes "already classified": rows scored by a superseded model
        no longer block re-scoring — the read view filters to the current model, so
        without this they'd silently vanish from the Incoming view after a re-fit
        until something re-scored them. ``None`` keeps the legacy any-model match.
        The ``cluster_labels`` subquery filters by ``run_id`` only (no target) —
        safe because run ids are globally unique (``new_run_id``). NOTE: this scans
        the full per-target tables each call (O(history)); fine for the manual
        button, but the streaming phase should bound it with a scored-watermark
        (see docs/online-classification-design.md)."""
        params: dict[str, Any] = {"t": target, "f": feature_set}
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
            f"SELECT toString(tx_hash) FROM {self._db}.transactions FINAL "
            "WHERE target = {t:String} "
            f"AND tx_hash NOT IN (SELECT tx_hash FROM {self._db}.tx_classifications FINAL "
            "WHERE target = {t:String} AND feature_set = {f:String}" + model_clause + ")"
            f"{run_clause} ORDER BY tx_hash"
        )
        return [str(r[0]) for r in self.client.query(sql, parameters=params).result_rows]
