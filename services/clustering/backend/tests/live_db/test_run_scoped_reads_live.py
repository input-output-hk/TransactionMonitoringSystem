"""Live validation that a run's reads survive the window moving past it.

The three reads ABOUT one cluster/anomaly run (its cluster summary, one
cluster's transactions, its ranked anomalies) join the run's stored membership
against a transaction relation. Under ``host_ch`` that relation used to be the
target's rolling "latest N transactions" window, which is correct only while the
run is still inside it. Once the target produces N newer transactions the join
matches nothing and all three come back empty, under run metadata still
reporting the run's clusters and flagged count. It does not recover on its own:
a replacement run only comes from a re-fit, and a stable model is never re-fit.

That is a join returning zero rows, not a query the server rejects, so a fake
client cannot see it: the hermetic suite pins the query text and the text was
never wrong. This tier executes the real join against a real server, with the
window deliberately narrower than the run, which is the only arrangement that
tells a correct read from the broken one.

The seed writes only the module's own tables under a throwaway UUID target; the
host arm of the hybrid union stays empty, so the server still parses, unifies
and executes the full text of every read under test.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.models import TxRecord, UtxoRecord
from app.storage.clickhouse import ClickHouseRepo
from app.storage.clickhouse.hybrid import HybridHistoryRepo

from .conftest import LIVE_NETWORK

# The run's population and the window that must NOT be able to hide it. The
# window is deliberately smaller, so the newest WINDOW_TXS transactions belong
# to nobody and every one of the run's own transactions has aged out of it:
# exactly the state a busy contract reaches days after its last re-fit.
RUN_TXS = 4
WINDOW_TXS = 3
CLUSTER_ID = 0


def _live_settings() -> Settings:
    return Settings(
        CHAIN_SOURCE="host_ch",
        HISTORY_SOURCE="blockfrost",
        BLOCKFROST_PROJECT_ID="livedbtest-key",
        CARDANO_NETWORK=LIVE_NETWORK,
        CLICKHOUSE_HOST=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        CLICKHOUSE_HTTP_PORT=int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123")),
        CLICKHOUSE_USER=os.environ.get("CLICKHOUSE_USER", "default"),
        CLICKHOUSE_PASSWORD=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        CLICKHOUSE_DB=os.environ.get("CLICKHOUSE_DB", "tms_clustering"),
        HOST_CLICKHOUSE_DB=os.environ.get("HOST_CLICKHOUSE_DB", "tms_analytics"),
        CLUSTERING_WINDOW_TXS=WINDOW_TXS,
    )


def _tx(target: str, tx_hash: str, slot: int) -> TxRecord:
    return TxRecord(
        target=target,
        target_type="address",
        tx_hash=tx_hash,
        block_height=slot,
        block_time=datetime(2023, 11, 14, tzinfo=UTC),
        slot=slot,
        fees=200_000,
        deposit=0,
        size=300,
        valid_contract=1,
        input_count=1,
        output_count=2,
        total_input_lovelace=1_000_000,
        total_output_lovelace=900_000,
        distinct_input_addresses=1,
        distinct_output_addresses=2,
        distinct_assets=0,
        redeemer_count=0,
    )


@pytest.fixture(scope="module")
def aged_out_run() -> tuple[Settings, str, str, str, list[str]]:
    """A run over the OLDEST transactions of a target that has since produced a
    full window of newer ones. Returns (settings, target, run_id, anomaly_run_id,
    the run's hashes)."""
    settings = _live_settings()
    target = f"livedbtest_{uuid.uuid4().hex[:16]}"
    run_id = f"shape-{uuid.uuid4().hex[:12]}"
    anomaly_run_id = f"anomaly-shape-{uuid.uuid4().hex[:12]}"

    # Oldest first: the run's transactions take the low slots, then a full
    # window of newer ones is written on top of them.
    run_hashes = [f"{i:02x}" * 32 for i in range(0x10, 0x10 + RUN_TXS)]
    newer_hashes = [f"{i:02x}" * 32 for i in range(0x40, 0x40 + WINDOW_TXS)]

    repo = ClickHouseRepo(settings)
    try:
        repo.insert_transactions(
            [_tx(target, h, slot=100 + i) for i, h in enumerate(run_hashes)]
            + [_tx(target, h, slot=900 + i) for i, h in enumerate(newer_hashes)]
        )
        repo.insert_utxos(
            [
                UtxoRecord(
                    target=target,
                    tx_hash=h,
                    role="output",
                    idx=0,
                    address=f"addr_{target}",
                    lovelace=1,
                )
                for h in run_hashes + newer_hashes
            ]
        )
        repo.save_cluster_run(
            {
                "run_id": run_id,
                "target": target,
                "feature_set": "shape",
                "eps": 0.5,
                "min_samples": 2,
                "metric": "euclidean",
                "n_points": RUN_TXS,
                "n_clusters": 1,
                "n_noise": 0,
                "silhouette": 0.5,
                "notes": "",
                "origin": "system",
            }
        )
        repo.save_cluster_labels(run_id, [(h, CLUSTER_ID) for h in run_hashes])
        repo.save_anomaly_run(
            {
                "run_id": anomaly_run_id,
                "target": target,
                "feature_set": "shape",
                "methods": "isolation_forest,lof,dbscan",
                "n_points": RUN_TXS,
                "n_flagged": RUN_TXS,
                "eps": 0.5,
                "min_samples": 2,
                "top_quantile": 0.05,
                "origin": "system",
            }
        )
        # (iso_score, lof_score, dbscan_noise, consensus, votes, score_rank)
        repo.save_anomaly_scores(
            anomaly_run_id,
            [(h, 0.9, 1.1, 1, 1.0, 3, i + 1) for i, h in enumerate(run_hashes)],
        )
    finally:
        repo.close()
    return settings, target, run_id, anomaly_run_id, run_hashes


def test_the_window_really_has_moved_past_the_run(
    aged_out_run: tuple[Settings, str, str, str, list[str]],
) -> None:
    """The premise of every assertion below: the target's window holds only the
    newer transactions, so a windowed join really would find none of the run's.
    Without this the sibling tests could pass on a window that never rolled."""
    settings, target, _run_id, _anomaly_run_id, run_hashes = aged_out_run
    repo = HybridHistoryRepo(settings)
    try:
        windowed = set(repo.fetch_shape_features(target)["tx_hash"])
        assert len(windowed) == WINDOW_TXS
        assert windowed.isdisjoint(run_hashes)
    finally:
        repo.close()


def test_run_scoped_reads_still_return_the_run(
    aged_out_run: tuple[Settings, str, str, str, list[str]],
) -> None:
    settings, target, run_id, anomaly_run_id, run_hashes = aged_out_run
    repo = HybridHistoryRepo(settings)
    try:
        summary = repo.cluster_summary(run_id, target)
        assert [r["cluster_id"] for r in summary] == [CLUSTER_ID]
        assert summary[0]["size"] == RUN_TXS

        members = repo.cluster_transactions(run_id, target, CLUSTER_ID, limit=100, offset=0)
        assert {r["tx_hash"] for r in members} == set(run_hashes)

        ranked = repo.top_anomalies(anomaly_run_id, target, limit=100)
        assert {r["tx_hash"] for r in ranked} == set(run_hashes)
    finally:
        repo.close()


def test_projection_rebuilds_the_run_feature_space(
    aged_out_run: tuple[Settings, str, str, str, list[str]],
) -> None:
    """The service-level half: the projection's feature matrix comes from the
    run's own membership, so it places every member rather than intersecting the
    current window to nothing."""
    from app.service.projection import build_projection

    settings, _target, run_id, _anomaly_run_id, run_hashes = aged_out_run
    repo = HybridHistoryRepo(settings)
    try:
        out = build_projection(repo, run_id, dims=2, limit=100)
        assert out["total"] == RUN_TXS
        assert {n["id"] for n in out["nodes"]} == set(run_hashes)
    finally:
        repo.close()
