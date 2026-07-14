"""Shared base for test fakes of the storage layer.

``FakeRepoBase`` implements every method of the ``Repo`` protocol
(app/storage/protocol.py) as ``raise NotImplementedError``, and the static
check at the bottom makes mypy fail if it drifts from the protocol. Per-test
fakes subclass it and override only the methods they exercise — so an
unexpected repo call fails loudly, and a repo-surface change shows up as a
type error here instead of a silently-stale fake.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

from app.models import AssetRecord, TxRecord, UtxoRecord
from app.storage.protocol import Repo


class FakeRepoBase:
    """Every ``Repo`` method, unimplemented. Subclass and override what you use."""

    # --- lifecycle ------------------------------------------------------------

    def ping(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # --- ingest ---------------------------------------------------------------

    def insert_transactions(self, rows: Sequence[TxRecord]) -> None:
        raise NotImplementedError

    def insert_utxos(self, rows: Sequence[UtxoRecord]) -> None:
        raise NotImplementedError

    def insert_assets(self, rows: Sequence[AssetRecord]) -> None:
        raise NotImplementedError

    def get_cursor(self, target: str) -> dict[str, Any] | None:
        raise NotImplementedError

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
        raise NotImplementedError

    def list_targets(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        raise NotImplementedError

    def count_targets(self) -> int:
        raise NotImplementedError

    def fetch_shape_features(self, target: str) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_tx_addresses(self, target: str) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_addresses_for_txs(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_shape_features_for(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame:
        raise NotImplementedError

    def count_transactions(self, target: str) -> int:
        raise NotImplementedError

    # --- cluster runs & labels -------------------------------------------------

    def save_cluster_run(self, run: dict[str, Any]) -> None:
        raise NotImplementedError

    def save_cluster_labels(self, run_id: str, labels: Sequence[tuple[str, int]]) -> None:
        raise NotImplementedError

    def list_runs(
        self, target: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def count_runs(self, target: str | None = None) -> int:
        raise NotImplementedError

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def latest_cluster_run(
        self, target: str, feature_set: str, *, near: str | None = None
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def latest_canonical_run(self, target: str, feature_set: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def cluster_summary(self, run_id: str, target: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def cluster_transactions(
        self, run_id: str, target: str, cluster_id: int, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def run_tx_labels(self, run_id: str) -> dict[str, int]:
        raise NotImplementedError

    def cluster_member_hashes(self, run_id: str, cluster_id: int) -> list[str]:
        raise NotImplementedError

    def set_tx_labels(
        self,
        target: str,
        tx_hashes: Sequence[str],
        label: str,
        *,
        source: str = "cluster",
        note: str = "",
    ) -> int:
        raise NotImplementedError

    def clear_tx_labels(self, target: str, tx_hashes: Sequence[str]) -> int:
        raise NotImplementedError

    def labels_for_target(self, target: str) -> dict[str, str]:
        raise NotImplementedError

    def cluster_labeled_hashes(self, target: str) -> set[str]:
        raise NotImplementedError

    # --- anomaly runs & scores ---------------------------------------------------

    def anomaly_votes_for_run(self, run_id: str) -> dict[str, int]:
        raise NotImplementedError

    def latest_anomaly_run(
        self, target: str, feature_set: str, *, near: str | None = None
    ) -> str | None:
        raise NotImplementedError

    def save_anomaly_run(self, run: dict[str, Any]) -> None:
        raise NotImplementedError

    def save_anomaly_scores(self, run_id: str, rows: Sequence[tuple[Any, ...]]) -> None:
        raise NotImplementedError

    def list_anomaly_runs(
        self, target: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def count_anomaly_runs(self, target: str | None = None) -> int:
        raise NotImplementedError

    def get_anomaly_run(self, run_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def delete_anomaly_run(self, run_id: str) -> None:
        raise NotImplementedError

    def top_anomalies(
        self, run_id: str, target: str, *, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    # --- contracts ----------------------------------------------------------------

    def save_contract(self, contract: dict[str, Any]) -> None:
        raise NotImplementedError

    def list_contracts(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        raise NotImplementedError

    def count_contracts(self) -> int:
        raise NotImplementedError

    def get_contract(self, target: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def update_contract_label(self, target: str, label: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def delete_contract(self, target: str) -> dict[str, Any]:
        raise NotImplementedError

    # --- jobs ------------------------------------------------------------------------

    def create_job(
        self,
        job_id: str,
        target: str,
        target_type: str,
        max_txs: int,
        reprocess: int,
        kind: str = "onboard",
    ) -> None:
        raise NotImplementedError

    def update_job(self, job_id: str, **changes: Any) -> None:
        raise NotImplementedError

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def list_jobs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        raise NotImplementedError

    def count_jobs(self) -> int:
        raise NotImplementedError

    def nonterminal_jobs(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    # --- online models & classifications ----------------------------------------------

    def save_cluster_model(self, model: dict[str, Any]) -> None:
        raise NotImplementedError

    def latest_cluster_model(self, target: str, feature_set: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def save_tx_classifications(self, rows: Sequence[Sequence[Any]]) -> int:
        raise NotImplementedError

    def online_noise_rate(
        self, target: str, feature_set: str, model_id: str, *, window: int = 500
    ) -> tuple[float, int]:
        raise NotImplementedError

    def latest_transactions(
        self, target: str, feature_set: str, *, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def unclassified_tx_hashes(
        self,
        target: str,
        feature_set: str,
        *,
        run_id: str | None = None,
        model_id: str | None = None,
    ) -> list[str]:
        raise NotImplementedError


def _drift_check() -> None:  # pragma: no cover - exists for mypy only
    """mypy fails here if ``FakeRepoBase`` drifts from the ``Repo`` protocol."""
    _: Repo = FakeRepoBase()


# --- Shared concrete fakes (used across service test modules) -----------------


class FakeGraphRepo(FakeRepoBase):
    """In-memory repo exposing exactly what the verdict-decorated graph read and
    the cluster-label writes touch. Shared by test_service_verdicts and
    test_service_labels."""

    def __init__(
        self,
        *,
        membership: dict[str, int],
        explicit: dict[str, str],
        votes: dict[str, int],
        anomaly_run: str | None = "an1",
    ) -> None:
        self._membership = membership
        self._explicit = explicit
        self._votes = votes
        self._anomaly_run = anomaly_run
        self.label_calls: list[tuple[str, list[str], str]] = []
        self.clear_calls: list[tuple[str, list[str]]] = []

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return {
            "run_id": run_id,
            "target": "addr",
            "feature_set": "shape",
            "created_at": "2024-01-01 00:00:00.000000",
        }

    def run_tx_labels(self, run_id: str) -> dict[str, int]:
        return dict(self._membership)

    def labels_for_target(self, target: str) -> dict[str, str]:
        return dict(self._explicit)

    def cluster_labeled_hashes(self, target: str) -> set[str]:
        # Default: treat the fixture's explicit labels as cluster-applied (propagating).
        return set(self._explicit)

    def latest_anomaly_run(
        self, target: str, feature_set: str, *, near: str | None = None
    ) -> str | None:
        return self._anomaly_run

    def anomaly_votes_for_run(self, run_id: str) -> dict[str, int]:
        return dict(self._votes)

    def fetch_addresses_for_txs(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame:
        return pd.DataFrame(columns=["tx_hash", "address"])

    def cluster_member_hashes(self, run_id: str, cluster_id: int) -> list[str]:
        return [tx for tx, cid in self._membership.items() if cid == cluster_id]

    def set_tx_labels(
        self,
        target: str,
        tx_hashes: Sequence[str],
        label: str,
        *,
        source: str = "cluster",
        note: str = "",
    ) -> int:
        self.label_calls.append((target, list(tx_hashes), label))
        return len(list(tx_hashes))

    def clear_tx_labels(self, target: str, tx_hashes: Sequence[str]) -> int:
        self.clear_calls.append((target, list(tx_hashes)))
        return len(list(tx_hashes))
