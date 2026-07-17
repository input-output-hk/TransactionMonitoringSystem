"""Parameter recommendation + the canonical ``process_contract`` pipeline.

``process_contract`` is exercised end-to-end against an in-memory repo and a
stub ``ChainSource`` — real sklearn runs on tiny canned features, no network/CH."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from app.ingest.ingester import IngestResult
from app.service import (
    FALLBACK_EPS,
    MIN_SAMPLES_FLOOR,
    _recommended_params,
    process_contract,
)
from app.sources.base import SourceNotFound, SourceRateLimited
from tests.fakes import FakeRepoBase


@pytest.fixture(autouse=True)
def _stub_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the host_ch path (the default), ``process_contract`` publishes the fit's
    contract_anomaly rows. Publishing has its own tests; stub it to a no-op here so
    these stay focused on the cluster/anomaly pipeline and the in-memory repo need
    not implement the publish read/write surface."""
    monkeypatch.setattr("app.service.publish.publish_contract_anomaly", lambda *a, **k: None)


def test_recommended_params_prefers_grid_recommendation() -> None:
    ev = {"recommended": {"eps": 1.5, "min_samples": 8}, "k_distance": {"knee_eps": 0.3}}
    assert _recommended_params(ev) == (1.5, 8)


def test_recommended_params_falls_back_to_knee_for_eps() -> None:
    ev = {"recommended": None, "k_distance": {"knee_eps": 0.42}}
    assert _recommended_params(ev) == (0.42, MIN_SAMPLES_FLOOR)


def test_recommended_params_uses_heuristic_when_nothing_available() -> None:
    ev = {"recommended": {}, "k_distance": {"knee_eps": None}}
    assert _recommended_params(ev) == (FALLBACK_EPS, MIN_SAMPLES_FLOOR)


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
        repo,
        target="addr1demo",
        target_type="address",
        max_txs=None,
        reprocess=True,
        job_id="job-1",
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


class FakeHostSource(FakeSource):
    """A host-backed stub (mirrors HostChainSource): its data already lives in
    storage, so onboarding must skip the download path even with reprocess=False."""

    host_backed = True


async def test_process_contract_host_backed_skips_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: adding an address on the host_ch sidecar enqueues a job with
    reprocess=False. A host-backed source has no download (fetch_tx raises), so
    the pipeline must NOT take the download path — it reads features from the host
    tables and completes. Previously this surfaced as "upstream data provider
    error" because ingest() reached host_ch.fetch_tx."""
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeHostSource())

    async def _boom(**kwargs: Any) -> IngestResult:
        raise AssertionError("download path must not run for a host-backed source")

    monkeypatch.setattr("app.service.pipeline.ingest", _boom)
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    result = await process_contract(
        repo,
        target="addr1demo",
        target_type="address",
        max_txs=None,
        reprocess=False,
        job_id="job-host",
    )
    assert result["tx_count"] == 8
    assert result["cluster_run_id"]
    assert repo.contracts[-1]["status"] == "done"
    assert repo.job_updates[-1][1]["status"] == "done"


async def test_process_contract_sets_registry_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target known to the vendored registry gets a human-readable label."""
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeSource())
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    # Minswap Order Contract script address (FakeSource ignores the value).
    addr = "addr1wxn9efv2f6w82hagxqtn62ju4m293tqvw0uhmdl64ch8uwc0h43gt"
    await process_contract(
        repo,
        target=addr,
        target_type="address",
        max_txs=None,
        reprocess=True,
        job_id="job-label",
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
        repo,
        target=addr,
        target_type="address",
        max_txs=None,
        reprocess=True,
        job_id="job-preset",
    )
    assert repo.contracts[-1]["label"] == "My Custom Name"  # not "Minswap Order Contract"


async def test_process_contract_skips_analysis_under_three_txs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeSource())
    repo = FakePipelineRepo(_shape_df(2), _addr_df(2))
    result = await process_contract(
        repo,
        target="addr1demo",
        target_type="address",
        max_txs=None,
        reprocess=True,
        job_id="job-2",
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
            repo,
            target="addr1demo",
            target_type="address",
            max_txs=100,
            reprocess=False,
            job_id="job-rl",
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
            repo,
            target="addr1missing",
            target_type="address",
            max_txs=None,
            reprocess=True,
            job_id="job-3",
        )
    assert repo.contracts[-1]["status"] == "failed"
    _job_id, changes = repo.job_updates[-1]
    assert changes["status"] == "failed"
    assert "not found" in changes["error"]


# --- the pre-deployment history stage -----------------------------------------


def _enable_history(monkeypatch: pytest.MonkeyPatch, flavor: str = "blockfrost") -> None:
    from app.config import get_settings

    monkeypatch.setenv("CHAIN_SOURCE", "host_ch")
    monkeypatch.setenv("HISTORY_SOURCE", flavor)
    if flavor == "blockfrost":
        monkeypatch.setenv("BLOCKFROST_PROJECT_ID", "k")
    else:
        monkeypatch.setenv("HOST_API_URL", "http://app:8000")
        monkeypatch.setenv("HOST_API_KEY", "secret")
    get_settings.cache_clear()


class _RecordingBackfill:
    """Stub HistoryBackfill capturing the pipeline's call."""

    def __init__(self, status: str = "completed", txs: int = 5) -> None:
        from app.service.history import HistoryResult

        self.calls: list[dict[str, Any]] = []
        self._result = HistoryResult(status, txs)  # type: ignore[arg-type]

    async def run(self, *, target: str, target_type: str, max_txs: int, progress: Any):
        self.calls.append({"target": target, "max_txs": max_txs})
        return self._result


def _patch_backfill(monkeypatch: pytest.MonkeyPatch, backfill: _RecordingBackfill) -> None:
    monkeypatch.setattr("app.service.pipeline.get_history_backfill", lambda s: backfill)


async def test_history_stage_runs_between_metadata_and_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_history(monkeypatch)
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeHostSource())
    backfill = _RecordingBackfill()
    _patch_backfill(monkeypatch, backfill)
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    result = await process_contract(
        repo, target="addr1demo", target_type="address", max_txs=None, reprocess=False
    )
    assert result["tx_count"] == 8
    # requested_max_txs is 0 for feed jobs → the configured default cap.
    assert backfill.calls == [{"target": "addr1demo", "max_txs": 500}]
    assert repo.contracts[-1]["status"] == "done"


async def test_history_stage_runs_under_reprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    # reprocess means "no primary re-download"; the cursor-guarded history stage
    # still runs — the refit is exactly the resume vehicle after a deferral.
    _enable_history(monkeypatch)
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeHostSource())
    backfill = _RecordingBackfill()
    _patch_backfill(monkeypatch, backfill)
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    await process_contract(
        repo, target="addr1demo", target_type="address", max_txs=None, reprocess=True
    )
    assert len(backfill.calls) == 1


async def test_history_deferral_does_not_fail_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_history(monkeypatch)
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeHostSource())
    _patch_backfill(monkeypatch, _RecordingBackfill(status="deferred", txs=0))
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    result = await process_contract(
        repo, target="addr1demo", target_type="address", max_txs=None, job_id="job-h"
    )
    # The fit proceeds on the host's tip-forward data; the job ends done.
    assert result["cluster_run_id"]
    assert repo.job_updates[-1][1]["status"] == "done"


async def test_no_history_stage_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeHostSource())
    backfill = _RecordingBackfill()
    _patch_backfill(monkeypatch, backfill)
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    await process_contract(repo, target="addr1demo", target_type="address", max_txs=None)
    assert backfill.calls == []


async def test_requested_max_txs_preserved_on_refit(monkeypatch: pytest.MonkeyPatch) -> None:
    # The feed's refit jobs carry max_txs=0; the persisted per-contract cap must
    # survive (it is the history stage's depth), like `label` does.
    _enable_history(monkeypatch)
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeHostSource())
    backfill = _RecordingBackfill()
    _patch_backfill(monkeypatch, backfill)
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    repo.contracts.append(
        {"target": "addr1demo", "target_type": "address", "requested_max_txs": 250, "label": ""}
    )
    await process_contract(
        repo, target="addr1demo", target_type="address", max_txs=None, reprocess=True
    )
    assert backfill.calls[0]["max_txs"] == 250
    assert repo.contracts[-1]["requested_max_txs"] == 250


async def test_too_few_txs_with_history_pending_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model-less `done` contract would never be re-onboarded by the feed;
    # with history still outstanding the contract must stay retryable.
    _enable_history(monkeypatch)
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeHostSource())
    _patch_backfill(monkeypatch, _RecordingBackfill(status="pending", txs=0))
    repo = FakePipelineRepo(_shape_df(2), _addr_df(2))
    result = await process_contract(
        repo, target="addr1demo", target_type="address", max_txs=None
    )
    assert result["note"] == "too few transactions for clustering/anomaly"
    assert repo.contracts[-1]["status"] == "pending"


async def test_too_few_txs_with_history_complete_marks_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_history(monkeypatch)
    monkeypatch.setattr("app.service.pipeline.get_source", lambda settings: FakeHostSource())
    _patch_backfill(monkeypatch, _RecordingBackfill(status="completed", txs=0))
    repo = FakePipelineRepo(_shape_df(2), _addr_df(2))
    await process_contract(repo, target="addr1demo", target_type="address", max_txs=None)
    assert repo.contracts[-1]["status"] == "done"


async def test_metadata_fallback_blockfrost(monkeypatch: pytest.MonkeyPatch) -> None:
    # Host-unknown target + blockfrost history → onboarding proceeds with the
    # provider's real metadata instead of failing SourceNotFound.
    _enable_history(monkeypatch)
    monkeypatch.setattr(
        "app.service.pipeline.get_source", lambda settings: FakeSourceMissingHost()
    )
    _patch_backfill(monkeypatch, _RecordingBackfill())

    class _BFStub:
        def __init__(self, settings: Any) -> None:
            pass

        async def __aenter__(self) -> _BFStub:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def metadata(self, target: str, target_type: str) -> dict[str, Any]:
            return {
                "exists": 1,
                "is_script": 1,
                "script_type": "plutusV2",
                "balance_lovelace": 7,
                "asset_count": 0,
                "sample_tokens": "[]",
            }

    monkeypatch.setattr("app.blockfrost.source.BlockfrostSource", _BFStub)
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    result = await process_contract(
        repo, target="addr1demo", target_type="address", max_txs=None
    )
    assert result["tx_count"] == 8
    assert repo.contracts[-1]["script_type"] == "plutusV2"


async def test_metadata_fallback_kupo_synthesizes_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kupo trigger cannot produce host rows synchronously, so the metadata
    # is synthesized from the address header (zero requests) and the
    # pending-retry loop finishes the job.
    _enable_history(monkeypatch, flavor="kupo")
    monkeypatch.setattr(
        "app.service.pipeline.get_source", lambda settings: FakeSourceMissingHost()
    )
    _patch_backfill(monkeypatch, _RecordingBackfill(status="pending", txs=0))
    repo = FakePipelineRepo(_shape_df(8), _addr_df(8))
    result = await process_contract(
        repo, target="addr1demo", target_type="address", max_txs=None
    )
    assert result["tx_count"] == 8
    assert repo.contracts[-1]["exists"] is True


class FakeSourceMissingHost(FakeSource):
    """Host-backed source for a target the host has no rows for."""

    host_backed = True

    async def metadata(self, target: str, target_type: str) -> dict[str, Any]:
        raise SourceNotFound("no rows for target")
