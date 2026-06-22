"""Incremental classification (fit/score split): lazy model build from the
canonical run, reuse while fresh, rebuild after a re-cluster."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from app.clustering.model import MODEL_SCHEMA_VERSION
from app.ingest.ingester import IngestResult
from app.service import classify_new_transactions, update_contract
from app.sources.base import SourceRateLimited
from tests.fakes import FakeRepoBase

# --- Incremental classification (fit/score) --------------------------------

_CLF_COLUMNS = [
    "tx_hash", "fees", "size", "input_count", "output_count",
    "total_input_lovelace", "total_output_lovelace", "net_lovelace",
    "distinct_assets", "redeemer_count", "hour_of_day", "day_of_week",
]


def _clf_row(tx: str, scale: float) -> dict:
    return {
        "tx_hash": tx, "fees": 150_000 + scale * 1000, "size": 300 + scale,
        "input_count": 1, "output_count": 2,
        "total_input_lovelace": int(1_000_000 * scale),
        "total_output_lovelace": int(990_000 * scale),
        "net_lovelace": -int(10_000 * scale), "distinct_assets": 0,
        "redeemer_count": 1, "hour_of_day": 12, "day_of_week": 3,
    }


class FakeClassifyRepo(FakeRepoBase):
    """Minimal in-memory repo for the classify_new_transactions wiring."""

    def __init__(self) -> None:
        train = [_clf_row(f"lo{i:02d}".ljust(64, "0"), 1.0 + i * 0.02) for i in range(8)]
        train += [_clf_row(f"hi{i:02d}".ljust(64, "0"), 50.0 + i * 0.02) for i in range(8)]
        self._train = pd.DataFrame(train, columns=_CLF_COLUMNS)
        self._cluster_of = {
            **{r["tx_hash"]: 0 for r in train[:8]},
            **{r["tx_hash"]: 1 for r in train[8:]},
        }
        self._new = pd.DataFrame(
            [_clf_row("new0".ljust(64, "0"), 1.05), _clf_row("new1".ljust(64, "0"), 9000.0)],
            columns=_CLF_COLUMNS,
        )
        self._model: dict | None = None
        self._run_id = "run-1"
        self.saved_classifications: list[tuple] = []
        self.model_saves = 0

    def latest_cluster_model(self, target: str, feature_set: str) -> dict | None:
        return self._model

    def latest_canonical_run(self, target: str, feature_set: str) -> dict | None:
        # The system-tuned run the model must fit from.
        return {"run_id": self._run_id, "eps": 0.5, "min_samples": 4}

    def latest_cluster_run(
        self, target: str, feature_set: str, *, near: str | None = None
    ) -> dict | None:
        # A newer *custom* run exists; it must NOT be chosen over the canonical one.
        return {"run_id": "run-2-custom", "eps": 0.7, "min_samples": 8}

    def run_tx_labels(self, run_id: str) -> dict[str, int]:
        return self._cluster_of

    def fetch_shape_features(self, target: str) -> pd.DataFrame:
        return self._train

    def labels_for_target(self, target: str) -> dict[str, str]:
        return {}

    def cluster_labeled_hashes(self, target: str) -> set[str]:
        return set()

    def save_cluster_model(self, model: dict) -> None:
        self.model_saves += 1
        self._model = model

    def unclassified_tx_hashes(
        self, target, feature_set, *, run_id=None, model_id=None
    ) -> list[str]:
        return self._new["tx_hash"].tolist()

    def fetch_shape_features_for(self, target, tx_hashes) -> pd.DataFrame:
        return self._new[self._new["tx_hash"].isin(set(tx_hashes))]

    def save_tx_classifications(self, rows) -> int:
        self.saved_classifications.extend(rows)
        return len(list(rows))

    def online_noise_rate(self, target, feature_set, model_id, *, window=500):
        rows = self.saved_classifications
        if not rows:
            return 0.0, 0
        noise = sum(1 for r in rows if r[4] == -1)  # cluster_id is column index 4
        return noise / len(rows), len(rows)


def test_classify_new_builds_model_then_scores_new_txs() -> None:
    repo = FakeClassifyRepo()
    out = classify_new_transactions(repo, "addr1demo")  # type: ignore[arg-type]

    assert out["n_new"] == 2
    assert repo.model_saves == 1  # model built lazily on first use
    # Scoring protection: the model is fit from the canonical run, not the newer custom one.
    assert repo._model["run_id"] == "run-1"
    assert len(repo.saved_classifications) == 2
    # The near point lands in a cluster; the extreme outlier is unassigned.
    cluster_ids = {row[1]: row[4] for row in repo.saved_classifications}  # tx_hash -> cluster_id
    assert cluster_ids["new0".ljust(64, "0")] in (0, 1)
    assert cluster_ids["new1".ljust(64, "0")] == -1
    # Drift sensor: one of the two new txs is unassigned → noise rate 0.5 over a window of 2.
    assert out["drift_score"] == 0.5
    assert out["drift_window_n"] == 2


def test_classify_new_reuses_existing_model() -> None:
    repo = FakeClassifyRepo()
    classify_new_transactions(repo, "addr1demo")  # type: ignore[arg-type]
    classify_new_transactions(repo, "addr1demo")  # type: ignore[arg-type]
    assert repo.model_saves == 1  # second call reuses the persisted model


def test_classify_new_rebuilds_model_after_recluster() -> None:
    repo = FakeClassifyRepo()
    classify_new_transactions(repo, "addr1demo")  # type: ignore[arg-type]
    assert repo.model_saves == 1
    repo._run_id = "run-2"  # a re-cluster produced a newer run
    classify_new_transactions(repo, "addr1demo")  # type: ignore[arg-type]
    assert repo.model_saves == 2  # stale model rebuilt from the new run


def test_classify_new_rebuilds_on_schema_version_bump() -> None:
    """A MODEL_SCHEMA_VERSION bump must invalidate a cached model even when the
    source run is unchanged, so a scoring-semantics change (e.g. the detector-only
    vote rule) re-fits and re-scores the online backlog instead of leaving stale
    rows untouched. Regression for the upgrade-path false-positive gap."""
    repo = FakeClassifyRepo()
    # Pre-seed a model fit from the current run but at an older schema version.
    repo._model = {
        "model_id": "old-model",
        "run_id": repo._run_id,
        "schema_version": MODEL_SCHEMA_VERSION - 1,
    }
    out = classify_new_transactions(repo, "addr1demo")  # type: ignore[arg-type]
    assert repo.model_saves == 1  # stale-version model rebuilt despite same run
    assert repo._model["model_id"] != "old-model"
    assert repo._model["schema_version"] == MODEL_SCHEMA_VERSION
    # The backlog is re-scored under the new model id (overwrites stale rows).
    assert out["n_new"] == 2 and out["model_id"] != "old-model"


# --- update_contract: rate-limited tip walk --------------------------------

class _FakeSource:
    """Stub ChainSource async context manager (download is patched out)."""

    def __init__(self, settings: Any = None, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeSource:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _JobRepo(FakeRepoBase):
    """Tracks job writes; classify is never reached on a rate-limited walk."""

    def __init__(self) -> None:
        self.job_updates: list[tuple[str, dict[str, Any]]] = []

    def update_job(self, job_id: str, **changes: Any) -> None:
        self.job_updates.append((job_id, changes))


class _DriftRepo(FakeClassifyRepo):
    """Full classify machinery plus the contract/job writes update_contract needs."""

    def __init__(self) -> None:
        super().__init__()
        self.job_updates: list[dict[str, Any]] = []
        self.saved_contract: dict[str, Any] | None = None

    def get_contract(self, target: str) -> dict[str, Any]:
        return {"target": target, "target_type": "address", "exists": 1}

    def count_transactions(self, target: str) -> int:
        return 18

    def save_contract(self, contract: dict[str, Any]) -> None:
        self.saved_contract = contract

    def update_job(self, job_id: str, **changes: Any) -> None:
        self.job_updates.append(changes)


async def test_update_contract_persists_drift_and_suggests_recluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed refresh stores the trailing noise rate on the contract and, when
    it crosses the threshold, surfaces a re-cluster recommendation in the job detail."""
    monkeypatch.setattr("app.service.online.get_source", lambda settings: _FakeSource())

    async def _completed(**kwargs: Any) -> IngestResult:
        return IngestResult("addr1demo", "address", "completed", 2, "page:1")

    monkeypatch.setattr("app.service.online.ingest", _completed)
    repo = _DriftRepo()
    out = await update_contract(
        repo,  # type: ignore[arg-type]
        target="addr1demo",
        target_type="address",
        job_id="job-d",
    )
    # 1 of 2 new txs is unassigned → 0.5, well over the 0.25 default threshold.
    assert out["drift_score"] == 0.5
    assert repo.saved_contract is not None and repo.saved_contract["drift_score"] == 0.5
    done = [c for c in repo.job_updates if c.get("status") == "done"][-1]
    assert "re-cluster recommended" in done["stage_detail"]


async def test_update_contract_rate_limited_fails_without_classifying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate-limited tip walk stops short of the tip; the incremental refresh
    must fail (resumable) rather than classify a partial catch-up and mark done."""
    monkeypatch.setattr("app.service.online.get_source", lambda settings: _FakeSource())

    async def _rate_limited(**kwargs: Any) -> IngestResult:
        return IngestResult("addr1demo", "address", "rate_limited", 3, "page:9")

    monkeypatch.setattr("app.service.online.ingest", _rate_limited)
    # If classify were reached it would need the full model machinery; this fake
    # has none, so any classify attempt would blow up loudly.
    repo = _JobRepo()
    with pytest.raises(SourceRateLimited):
        await update_contract(
            repo, target="addr1demo", target_type="address", job_id="job-rl",
        )
    _job_id, changes = repo.job_updates[-1]
    assert changes["status"] == "failed"
    assert "request limit" in changes["error"]
