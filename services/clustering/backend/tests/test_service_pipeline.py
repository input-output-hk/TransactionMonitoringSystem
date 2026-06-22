"""Parameter recommendation + the canonical ``process_contract`` pipeline.

``process_contract`` is exercised end-to-end against an in-memory repo and a
stub ``ChainSource`` — real sklearn runs on tiny canned features, no network/CH."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from app.ingest.ingester import IngestResult
from app.service import (
    _FALLBACK_EPS,
    _FALLBACK_MIN_SAMPLES,
    _recommended_params,
    process_contract,
)
from app.sources.base import SourceNotFound, SourceRateLimited
from tests.fakes import FakeRepoBase


def test_recommended_params_prefers_grid_recommendation() -> None:
    ev = {"recommended": {"eps": 1.5, "min_samples": 8}, "k_distance": {"knee_eps": 0.3}}
    assert _recommended_params(ev) == (1.5, 8)


def test_recommended_params_falls_back_to_knee_for_eps() -> None:
    ev = {"recommended": None, "k_distance": {"knee_eps": 0.42}}
    assert _recommended_params(ev) == (0.42, _FALLBACK_MIN_SAMPLES)


def test_recommended_params_uses_heuristic_when_nothing_available() -> None:
    ev = {"recommended": {}, "k_distance": {"knee_eps": None}}
    assert _recommended_params(ev) == (_FALLBACK_EPS, _FALLBACK_MIN_SAMPLES)


def test_recommended_params_returns_float_and_int() -> None:
    eps, min_samples = _recommended_params(
        {"recommended": {"eps": 2, "min_samples": 5}, "k_distance": {"knee_eps": 1.0}}
    )
    assert isinstance(eps, float) and isinstance(min_samples, int)


# --- process_contract integration ------------------------------------------

def _shape_df(n: int) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "tx_hash": f"{i:064x}",
                "fees": 150_000 + i * 1_000,
                "size": 300 + i * 5,
                "input_count": 1 + (i % 3),
                "output_count": 2 + (i % 2),
                "total_input_lovelace": 1_000_000 + i * 10_000,
                "total_output_lovelace": 900_000 + i * 9_000,
                "net_lovelace": -100_000 - i * 100,
                "distinct_assets": i % 2,
                "redeemer_count": 1,
                "hour_of_day": i % 24,
                "day_of_week": (i % 7) + 1,
            }
            for i in range(n)
        ]
    )


def _addr_df(n: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for i in range(n):
        tx = f"{i:064x}"
        rows.append({"tx_hash": tx, "address": "addrShared"})
        rows.append({"tx_hash": tx, "address": f"addr{i % 3}"})
    return pd.DataFrame(rows)


class FakePipelineRepo(FakeRepoBase):
    """In-memory repo exposing exactly what process_contract touches."""

    def __init__(self, shape_df: pd.DataFrame, addr_df: pd.DataFrame) -> None:
        self._shape = shape_df
        self._addr = addr_df
        self.contracts: list[dict[str, Any]] = []
        self.cluster_runs: list[dict[str, Any]] = []
        self.anomaly_runs: list[dict[str, Any]] = []
        self.job_updates: list[tuple[str, dict[str, Any]]] = []

    def fetch_shape_features(self, target: str) -> pd.DataFrame:
        return self._shape.copy()

    def fetch_tx_addresses(self, target: str) -> pd.DataFrame:
        return self._addr.copy()

    def save_contract(self, contract: dict[str, Any]) -> None:
        self.contracts.append(dict(contract))

    def save_cluster_run(self, run: dict[str, Any]) -> None:
        self.cluster_runs.append(run)

    def save_cluster_labels(self, run_id: str, labels: Any) -> None:
        pass

    def save_anomaly_run(self, run: dict[str, Any]) -> None:
        self.anomaly_runs.append(run)

    def save_anomaly_scores(self, run_id: str, rows: Any) -> None:
        pass

    def update_job(self, job_id: str, **changes: Any) -> None:
        self.job_updates.append((job_id, changes))

    def get_contract(self, target: str) -> dict[str, Any] | None:
        return self.contracts[-1] if self.contracts else None


class FakeSource:
    """Stub ChainSource: an existing script address with no tokens. Only
    ``metadata`` is exercised here — every pipeline test uses ``reprocess=True``,
    so the download path (tx_hash_pages/fetch_tx) is never called."""

    def __init__(self, settings: Any = None, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> FakeSource:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def metadata(self, target: str, target_type: str) -> dict[str, Any]:
        return {
            "exists": 1,
            "is_script": 1,
            "script_type": "",
            "balance_lovelace": 5_000_000,
            "asset_count": 0,
            "sample_tokens": "[]",
        }


class FakeSourceMissing(FakeSource):
    async def metadata(self, target: str, target_type: str) -> dict[str, Any]:
        raise SourceNotFound("404")


async def test_process_contract_reprocess_runs_full_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeSource())
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    result = await process_contract(
        repo, target="addr1demo", target_type="address",
        max_txs=None, reprocess=True, job_id="job-1",
    )
    assert result["tx_count"] == 8
    assert result["cluster_run_id"]
    assert result["shape_anomaly_run_id"].startswith("anomaly-shape-")
    assert result["graph_anomaly_run_id"].startswith("anomaly-graph-")
    # One shape cluster + shape & graph anomaly runs persisted.
    assert len(repo.cluster_runs) == 1
    # The auto-tuned onboarding run is the canonical (system-tuned) run.
    assert repo.cluster_runs[0]["origin"] == "system"
    assert {r["feature_set"] for r in repo.anomaly_runs} == {"shape", "graph"}
    # Onboarding-produced anomaly runs are system-tagged (not user-deletable).
    assert all(r["origin"] == "system" for r in repo.anomaly_runs)
    # Contract went processing → done; job ended done.
    assert "processing" in [c["status"] for c in repo.contracts]
    assert repo.contracts[-1]["status"] == "done"
    assert repo.contracts[-1]["tx_count"] == 8
    assert repo.job_updates[-1][1]["status"] == "done"
    # Unknown address → registry label stays empty (no regression).
    assert repo.contracts[-1]["label"] == ""


async def test_process_contract_sets_registry_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target known to the vendored registry gets a human-readable label."""
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeSource())
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    # Minswap Order Contract script address (FakeSource ignores the value).
    addr = "addr1wxn9efv2f6w82hagxqtn62ju4m293tqvw0uhmdl64ch8uwc0h43gt"
    await process_contract(
        repo, target=addr, target_type="address",
        max_txs=None, reprocess=True, job_id="job-label",
    )
    assert repo.contracts[-1]["label"] == "Minswap Order Contract"


async def test_process_contract_preserves_preset_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preset label (user-supplied or pre-existing) wins over the registry."""
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeSource())
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    # Seed an existing row with a custom name for this target.
    addr = "addr1wxn9efv2f6w82hagxqtn62ju4m293tqvw0uhmdl64ch8uwc0h43gt"  # Minswap order
    repo.contracts.append({"target": addr, "label": "My Custom Name"})
    await process_contract(
        repo, target=addr, target_type="address",
        max_txs=None, reprocess=True, job_id="job-preset",
    )
    assert repo.contracts[-1]["label"] == "My Custom Name"  # not "Minswap Order Contract"


async def test_process_contract_skips_analysis_under_three_txs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeSource())
    repo = FakePipelineRepo(_shape_df(2), _addr_df(2))
    result = await process_contract(
        repo, target="addr1demo", target_type="address",
        max_txs=None, reprocess=True, job_id="job-2",
    )
    assert "note" in result
    assert repo.cluster_runs == [] and repo.anomaly_runs == []
    assert repo.contracts[-1]["status"] == "done"


async def test_process_contract_rate_limited_download_fails_no_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate-limited (partial) download must NOT be clustered or marked done:
    the job fails (resumable from the saved cursor) instead of reporting a false
    'done' built on incomplete chain data."""
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeSource())

    async def _rate_limited(**kwargs: Any) -> IngestResult:
        return IngestResult("addr1demo", "address", "rate_limited", 5, "page:2")

    monkeypatch.setattr("app.service.pipeline.ingest", _rate_limited)
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    with pytest.raises(SourceRateLimited):
        await process_contract(
            repo, target="addr1demo", target_type="address",
            max_txs=100, reprocess=False, job_id="job-rl",
        )
    # Nothing clustered/scored, contract + job both failed (not done).
    assert repo.cluster_runs == [] and repo.anomaly_runs == []
    assert repo.contracts[-1]["status"] == "failed"
    _job_id, changes = repo.job_updates[-1]
    assert changes["status"] == "failed"
    assert "request limit" in changes["error"]


async def test_process_contract_failure_marks_failed_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeSourceMissing())
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    with pytest.raises(SourceNotFound):
        await process_contract(
            repo, target="addr1missing", target_type="address",
            max_txs=None, reprocess=True, job_id="job-3",
        )
    assert repo.contracts[-1]["status"] == "failed"
    _job_id, changes = repo.job_updates[-1]
    assert changes["status"] == "failed"
    assert "not found" in changes["error"]
