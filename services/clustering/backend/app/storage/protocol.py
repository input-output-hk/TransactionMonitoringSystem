"""The storage protocol the engine programs against.

``Repo`` is the full data surface used by the service layer, ingester, jobs and
API — and nothing more: no ``settings``, no ClickHouse client. Callers that need
configuration take it as parameters or read ``get_settings()`` themselves, so an
alternative storage backend only has to implement data access.

``ClickHouseRepo`` is the (only) production implementation; a static assignment
in ``app/storage/clickhouse/__init__.py`` makes mypy fail if it drifts from this
Protocol. Test fakes subclass ``tests/fakes.py:FakeRepoBase`` (every method
raises ``NotImplementedError``) and override only what they exercise, so fakes
are type-checked against the same surface.

Sections mirror the ClickHouseRepo mixins (``storage/clickhouse/*.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import pandas as pd

from app.models import AssetRecord, TxRecord, UtxoRecord


class Repo(Protocol):
    """Everything the engine needs from storage. See module docstring."""

    # --- lifecycle (base.py) ------------------------------------------------

    def ping(self) -> bool: ...

    def close(self) -> None: ...

    # --- ingest (ingest.py) -------------------------------------------------

    def insert_transactions(self, rows: Sequence[TxRecord]) -> None: ...

    def insert_utxos(self, rows: Sequence[UtxoRecord]) -> None: ...

    def insert_assets(self, rows: Sequence[AssetRecord]) -> None: ...

    def get_cursor(self, target: str) -> dict[str, Any] | None: ...

    def upsert_cursor(
        self,
        target: str,
        target_type: str,
        *,
        cursor: str,
        last_tx_hash: str,
        txs_seen: int,
        done: bool,
    ) -> None: ...

    def list_targets(self) -> list[dict[str, Any]]: ...

    def fetch_shape_features(self, target: str) -> pd.DataFrame: ...

    def fetch_tx_addresses(self, target: str) -> pd.DataFrame: ...

    def fetch_addresses_for_txs(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame: ...

    def fetch_shape_features_for(self, target: str, tx_hashes: Sequence[str]) -> pd.DataFrame: ...

    def count_transactions(self, target: str) -> int: ...

    # --- cluster runs & labels (clusters.py) ---------------------------------

    def save_cluster_run(self, run: dict[str, Any]) -> None: ...

    def save_cluster_labels(self, run_id: str, labels: Sequence[tuple[str, int]]) -> None: ...

    def list_runs(self, target: str | None = None) -> list[dict[str, Any]]: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def latest_cluster_run(
        self, target: str, feature_set: str, *, near: str | None = None
    ) -> dict[str, Any] | None: ...

    def latest_canonical_run(self, target: str, feature_set: str) -> dict[str, Any] | None: ...

    def cluster_summary(self, run_id: str, target: str) -> list[dict[str, Any]]: ...

    def cluster_transactions(
        self, run_id: str, target: str, cluster_id: int, *, limit: int, offset: int
    ) -> list[dict[str, Any]]: ...

    def run_tx_labels(self, run_id: str) -> dict[str, int]: ...

    def cluster_member_hashes(self, run_id: str, cluster_id: int) -> list[str]: ...

    def set_tx_labels(
        self,
        target: str,
        tx_hashes: Sequence[str],
        label: str,
        *,
        source: str = "cluster",
        note: str = "",
    ) -> int: ...

    def clear_tx_labels(self, target: str, tx_hashes: Sequence[str]) -> int: ...

    def labels_for_target(self, target: str) -> dict[str, str]: ...

    def cluster_labeled_hashes(self, target: str) -> set[str]: ...

    # --- anomaly runs & scores (anomaly.py) ----------------------------------

    def anomaly_votes_for_run(self, run_id: str) -> dict[str, int]: ...

    def latest_anomaly_run(
        self, target: str, feature_set: str, *, near: str | None = None
    ) -> str | None: ...

    def save_anomaly_run(self, run: dict[str, Any]) -> None: ...

    def save_anomaly_scores(self, run_id: str, rows: Sequence[tuple[Any, ...]]) -> None: ...

    def list_anomaly_runs(self, target: str | None = None) -> list[dict[str, Any]]: ...

    def get_anomaly_run(self, run_id: str) -> dict[str, Any] | None: ...

    def delete_anomaly_run(self, run_id: str) -> None: ...

    def top_anomalies(
        self, run_id: str, target: str, *, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]: ...

    # --- contracts (contracts.py) ---------------------------------------------

    def save_contract(self, contract: dict[str, Any]) -> None: ...

    def list_contracts(self) -> list[dict[str, Any]]: ...

    def get_contract(self, target: str) -> dict[str, Any] | None: ...

    def update_contract_label(self, target: str, label: str) -> dict[str, Any] | None: ...

    def delete_contract(self, target: str) -> dict[str, Any]: ...

    # --- jobs (jobs.py) --------------------------------------------------------

    def create_job(
        self,
        job_id: str,
        target: str,
        target_type: str,
        max_txs: int,
        reprocess: int,
        kind: str = "onboard",
    ) -> None: ...

    def update_job(self, job_id: str, **changes: Any) -> None: ...

    def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    def list_jobs(self) -> list[dict[str, Any]]: ...

    def nonterminal_jobs(self) -> list[dict[str, Any]]: ...

    # --- online models & classifications (models.py) ---------------------------

    def save_cluster_model(self, model: dict[str, Any]) -> None: ...

    def latest_cluster_model(self, target: str, feature_set: str) -> dict[str, Any] | None: ...

    def save_tx_classifications(self, rows: Sequence[Sequence[Any]]) -> int: ...

    def online_noise_rate(
        self, target: str, feature_set: str, model_id: str, *, window: int = 500
    ) -> tuple[float, int]: ...

    def latest_transactions(
        self, target: str, feature_set: str, *, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]: ...

    def unclassified_tx_hashes(
        self,
        target: str,
        feature_set: str,
        *,
        run_id: str | None = None,
        model_id: str | None = None,
    ) -> list[str]: ...
