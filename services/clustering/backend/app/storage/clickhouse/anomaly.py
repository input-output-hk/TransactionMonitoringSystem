"""Anomaly-detection runs and per-tx ensemble scores."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .base import _RepoBase, _row_to_dict
from .ingest import TX_CONTEXT_KEYS, TX_CONTEXT_SELECT

_ANOMALY_RUN_OUT_KEYS = [
    "run_id",
    "target",
    "feature_set",
    "methods",
    "n_points",
    "n_flagged",
    "eps",
    "min_samples",
    "top_quantile",
    "origin",
    "created_at",
]
_ANOMALY_RUN_INT_KEYS = ("n_points", "n_flagged", "min_samples")


class _AnomalyMixin(_RepoBase):
    """Anomaly run/score persistence + reads."""

    def anomaly_votes_for_run(self, run_id: str) -> dict[str, int]:
        """``{tx_hash: votes}`` for an anomaly run, for joining the auto-flag state.

        Returns the whole run (a cheap columnar read); callers that only need a few
        rows still take this rather than pass a large ``IN`` hash array as a query
        parameter, which would overflow ClickHouse's HTTP form-field limit."""
        rows = self.client.query(
            f"SELECT toString(tx_hash), votes FROM {self._db}.anomaly_scores FINAL "
            f"WHERE run_id = {{r:String}}",
            parameters={"r": run_id},
        ).result_rows
        return {str(tx): int(v) for (tx, v) in rows}

    def latest_anomaly_run(
        self, target: str, feature_set: str, *, near: str | None = None
    ) -> str | None:
        """run_id of an anomaly run for ``(target, feature_set)``, or ``None``.

        Cluster and anomaly runs are separate run_ids produced together by
        ``process_contract``. With ``near`` (a cluster run's ``created_at``) we pick
        the anomaly run closest in time — i.e. the one produced alongside that
        cluster run — so viewing an older cluster run doesn't pull votes from a
        later manual anomaly run. Without it we fall back to the most recent."""
        where = "WHERE target = {t:String} AND feature_set = {fs:String}"
        params: dict[str, Any] = {"t": target, "fs": feature_set}
        if near:
            # created_at is second-precision DateTime; pick the run closest in time
            # to the cluster run (its sibling). parseDateTimeBestEffort tolerates the
            # stringified timestamp format regardless of fractional seconds. The
            # secondary created_at DESC makes equidistant ties (two runs the same
            # number of seconds away) deterministic — newest wins.
            order = (
                "ORDER BY abs(dateDiff('second', created_at, "
                "parseDateTimeBestEffort({near:String}))) ASC, created_at DESC"
            )
            params["near"] = near
        else:
            order = "ORDER BY created_at DESC"
        rows = self.client.query(
            f"SELECT run_id FROM {self._db}.anomaly_runs FINAL {where} {order} LIMIT 1",
            parameters=params,
        ).result_rows
        return str(rows[0][0]) if rows else None

    def save_anomaly_run(self, run: dict[str, Any]) -> None:
        cols = [
            "run_id",
            "target",
            "feature_set",
            "methods",
            "n_points",
            "n_flagged",
            "eps",
            "min_samples",
            "top_quantile",
            "origin",
        ]
        defaults = {"origin": "custom"}
        self._insert("anomaly_runs", cols, [[run.get(c, defaults.get(c)) for c in cols]])

    def save_anomaly_scores(self, run_id: str, rows: Sequence[tuple[Any, ...]]) -> None:
        cols = [
            "run_id",
            "tx_hash",
            "iso_score",
            "lof_score",
            "dbscan_noise",
            "consensus",
            "votes",
            "score_rank",
        ]
        self._insert("anomaly_scores", cols, [[run_id, *r] for r in rows])

    _ANOMALY_RUN_SELECT = (
        "SELECT run_id, target, feature_set, methods, n_points, n_flagged, "
        "eps, min_samples, top_quantile, origin, toString(created_at) AS created_at "
        "FROM {db}.anomaly_runs FINAL {where}"
    )

    @staticmethod
    def _anomaly_run_to_dict(r: Sequence[Any]) -> dict[str, Any]:
        return _row_to_dict(
            _ANOMALY_RUN_OUT_KEYS,
            r,
            int_keys=_ANOMALY_RUN_INT_KEYS,
            float_keys=("eps", "top_quantile"),
        )

    def list_anomaly_runs(
        self, target: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        where = "WHERE target = {t:String}" if target else ""
        sql = self._ANOMALY_RUN_SELECT.format(db=self._db, where=where) + (
            " ORDER BY created_at DESC LIMIT {lim:UInt32} OFFSET {off:UInt32}"
        )
        params: dict[str, Any] = {"lim": limit, "off": offset}
        if target:
            params["t"] = target
        rows = self.client.query(sql, parameters=params).result_rows
        return [self._anomaly_run_to_dict(r) for r in rows]

    def count_anomaly_runs(self, target: str | None = None) -> int:
        """Full (unpaginated) anomaly-run count backing the list envelope's
        ``total``. ``count() AS total`` is safe under the 26.x alias rule: single
        aggregate, and ``anomaly_runs`` has no ``total`` source column."""
        where = "WHERE target = {t:String}" if target else ""
        rows = self.client.query(
            f"SELECT count() AS total FROM {self._db}.anomaly_runs FINAL {where}",
            parameters={"t": target} if target else None,
        ).result_rows
        return int(rows[0][0]) if rows else 0

    def get_anomaly_run(self, run_id: str) -> dict[str, Any] | None:
        sql = (
            self._ANOMALY_RUN_SELECT.format(db=self._db, where="WHERE run_id = {r:String}")
            + " LIMIT 1"
        )
        rows = self.client.query(sql, parameters={"r": run_id}).result_rows
        return self._anomaly_run_to_dict(rows[0]) if rows else None

    def delete_anomaly_run(self, run_id: str) -> None:
        """Hard-purge an anomaly run and its per-tx scores.

        Real row deletes (``ALTER … DELETE``, ``mutations_sync = 2``) like
        ``delete_contract`` — the run and its scores are gone when this returns.
        Origin/existence checks belong to the caller (the API guards system runs)."""
        for table in ("anomaly_scores", "anomaly_runs"):
            self.client.command(
                f"ALTER TABLE {self._db}.{table} DELETE WHERE run_id = {{r:String}} "
                f"SETTINGS mutations_sync = 2",
                parameters={"r": run_id},
            )

    def top_anomalies(
        self, run_id: str, target: str, *, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = self.client.query(
            f"""
            SELECT s.score_rank AS score_rank, toString(s.tx_hash) AS tx_hash,
                   s.consensus AS consensus, s.votes AS votes,
                   s.iso_score AS iso_score, s.lof_score AS lof_score,
                   s.dbscan_noise AS dbscan_noise,
                   {TX_CONTEXT_SELECT},
                   toHour(t.block_time) AS hour_of_day,
                   toDayOfWeek(t.block_time) AS day_of_week
            FROM (SELECT * FROM {self._db}.anomaly_scores FINAL WHERE run_id = {{r:String}}) s
            INNER JOIN (SELECT * FROM {self._db}.transactions FINAL WHERE target = {{t:String}}) t
                USING (tx_hash)
            ORDER BY s.score_rank ASC
            LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}
            """,
            parameters={"r": run_id, "t": target, "lim": limit, "off": offset},
        ).result_rows
        keys = [
            "score_rank",
            "tx_hash",
            "consensus",
            "votes",
            "iso_score",
            "lof_score",
            "dbscan_noise",
            *TX_CONTEXT_KEYS,
            "hour_of_day",
            "day_of_week",
        ]
        return self._rows_to_dicts(keys, rows, nan_none_keys=("iso_score",))
