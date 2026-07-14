"""Background onboarding/refresh job rows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .base import _RepoBase, _row_to_dict

# Columns written for a job row (``updated_at`` defaults to now64 on insert so it
# advances on every write; ``created_at`` is preserved across updates).
JOB_COLUMNS = [
    "job_id",
    "target",
    "target_type",
    "max_txs",
    "reprocess",
    "kind",
    "status",
    "stage_detail",
    "txs_done",
    "error",
    "created_at",
]
_TERMINAL_JOB_STATUSES = ("done", "failed")

_JOB_OUT_KEYS = [
    "job_id",
    "target",
    "target_type",
    "max_txs",
    "reprocess",
    "kind",
    "status",
    "stage_detail",
    "txs_done",
    "error",
    "created_at",
    "updated_at",
]
_JOB_INT_KEYS = ("max_txs", "reprocess", "txs_done")


class _JobMixin(_RepoBase):
    """Job creation, read-modify-write updates, and listing."""

    def create_job(
        self,
        job_id: str,
        target: str,
        target_type: str,
        max_txs: int,
        reprocess: int,
        kind: str = "onboard",
    ) -> None:
        self._insert(
            "jobs",
            ["job_id", "target", "target_type", "max_txs", "reprocess", "kind", "status"],
            [[job_id, target, target_type, int(max_txs), int(reprocess), kind, "queued"]],
        )

    def _job_row(self, job_id: str) -> dict[str, Any] | None:
        """Latest job row with native-typed columns (``created_at`` as datetime),
        used for read-modify-write updates that must preserve ``created_at``."""
        rows = self.client.query(
            f"SELECT {', '.join(JOB_COLUMNS)} FROM {self._db}.jobs FINAL "
            f"WHERE job_id = {{j:String}} LIMIT 1",
            parameters={"j": job_id},
        ).result_rows
        return self._rows_to_dicts(JOB_COLUMNS, rows)[0] if rows else None

    def update_job(self, job_id: str, **changes: Any) -> None:
        """Merge ``changes`` into the current job row and re-insert it.

        ``updated_at`` is omitted so the server stamps a fresh now64(6) on each
        write (the ReplacingMergeTree then keeps this newest row). This is a
        read-modify-write, so it assumes a SINGLE writer per job — guaranteed by
        the one JobManager worker thread; microsecond precision plus the
        round-trip between writes makes version ties effectively impossible.
        """
        cur = self._job_row(job_id)
        if cur is None:
            raise KeyError(job_id)
        cur.update(changes)
        self._insert("jobs", JOB_COLUMNS, [[cur[c] for c in JOB_COLUMNS]])

    @staticmethod
    def _job_to_dict(r: Sequence[Any]) -> dict[str, Any]:
        return _row_to_dict(_JOB_OUT_KEYS, r, int_keys=_JOB_INT_KEYS)

    _JOB_SELECT = (
        "SELECT job_id, target, target_type, max_txs, reprocess, kind, status, "
        "stage_detail, txs_done, error, toString(created_at) AS created_at, "
        "toString(updated_at) AS updated_at FROM {db}.jobs FINAL {where}"
    )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        sql = self._JOB_SELECT.format(db=self._db, where="WHERE job_id = {j:String}") + " LIMIT 1"
        rows = self.client.query(sql, parameters={"j": job_id}).result_rows
        return self._job_to_dict(rows[0]) if rows else None

    def list_jobs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        sql = self._JOB_SELECT.format(db=self._db, where="") + (
            " ORDER BY created_at DESC LIMIT {lim:UInt32} OFFSET {off:UInt32}"
        )
        rows = self.client.query(sql, parameters={"lim": limit, "off": offset}).result_rows
        return [self._job_to_dict(r) for r in rows]

    def count_jobs(self) -> int:
        """Full (unpaginated) job count backing the list envelope's ``total``.
        ``count() AS total`` is safe under the 26.x alias rule: single aggregate,
        and ``jobs`` has no ``total`` source column."""
        rows = self.client.query(f"SELECT count() AS total FROM {self._db}.jobs FINAL").result_rows
        return int(rows[0][0]) if rows else 0

    def nonterminal_jobs(self) -> list[dict[str, Any]]:
        statuses = ", ".join(f"'{s}'" for s in _TERMINAL_JOB_STATUSES)
        sql = self._JOB_SELECT.format(db=self._db, where=f"WHERE status NOT IN ({statuses})")
        return [self._job_to_dict(r) for r in self.client.query(sql).result_rows]
