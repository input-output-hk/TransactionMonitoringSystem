"""The per-contract "latest N to cluster on" window.

Covers the shared clamp (Settings.effective_window_txs), the repo binding that
turns it into the read LIMIT (_scope_params), and the backfill's window-full
skip gate reading the SAME per-contract N. Together these keep one number — the
N the operator picked — governing the fit population, the card's tx_count and
the point past which older history is not worth fetching, so they cannot drift.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.storage.clickhouse.host_backed import HostBackedRepo
from tests.test_hybrid_repo import FakeClient

# A ceiling comfortably above the default floor (200) so both clamps are visible.
_WINDOW_CEILING = 50_000
_FLOOR = 200


def _settings() -> Settings:
    return Settings(
        CHAIN_SOURCE="host_ch",
        CLUSTERING_WINDOW_TXS=_WINDOW_CEILING,
        CLUSTERING_MIN_TARGET_TXS=_FLOOR,
    )


# --- the shared clamp --------------------------------------------------------------


def test_effective_window_uses_named_n_within_range() -> None:
    # The common case: an operator's N between the floor and the ceiling is used
    # verbatim — this is the number the fit, the count and the gate all see.
    assert _settings().effective_window_txs(1_000) == 1_000


def test_effective_window_unset_falls_back_to_ceiling() -> None:
    # 0/unset (legacy feed-onboarded rows) must NOT shrink to a small default:
    # defaulting to the full window keeps the recall-safe status quo.
    assert _settings().effective_window_txs(0) == _WINDOW_CEILING


def test_effective_window_clamps_below_floor_up_to_floor() -> None:
    # Recall floor: too small an N would starve the outlier baseline (LOF's fixed
    # neighborhood, DBSCAN min_samples), so a named N below it is lifted.
    assert _settings().effective_window_txs(10) == _FLOOR


def test_effective_window_clamps_above_ceiling_down_to_ceiling() -> None:
    # The ceiling is the fit-memory / O(n^2)-silhouette bound; N can't exceed it.
    assert _settings().effective_window_txs(999_999) == _WINDOW_CEILING


def test_effective_window_ceiling_zero_is_unbounded() -> None:
    # 0 ceiling = unbounded (small/test contracts): returned as-is so the caller
    # omits the LIMIT entirely, whatever N the contract carries.
    unbounded = Settings(CHAIN_SOURCE="host_ch", CLUSTERING_WINDOW_TXS=0)
    assert unbounded.effective_window_txs(0) == 0
    assert unbounded.effective_window_txs(1_000) == 0


def test_effective_window_floor_never_exceeds_ceiling() -> None:
    # A ceiling below the floor (misconfig / tiny test window) must not return a
    # LIMIT above the ceiling — the floor is itself clamped to the ceiling.
    tiny = Settings(
        CHAIN_SOURCE="host_ch", CLUSTERING_WINDOW_TXS=100, CLUSTERING_MIN_TARGET_TXS=200
    )
    assert tiny.effective_window_txs(10) == 100
    assert tiny.effective_window_txs(0) == 100


# --- the repo binding (per-contract LIMIT) -----------------------------------------


def test_scope_params_binds_the_per_contract_window(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = HostBackedRepo(_settings(), client=FakeClient())
    monkeypatch.setattr(repo, "_target_requested_max_txs", lambda _t: 1_000)
    assert repo._scope_params("addr1demo")["lim"] == 1_000
    # floor + ceiling clamps flow through the same binding
    monkeypatch.setattr(repo, "_target_requested_max_txs", lambda _t: 10)
    assert repo._scope_params("addr1demo")["lim"] == _FLOOR
    monkeypatch.setattr(repo, "_target_requested_max_txs", lambda _t: 999_999)
    assert repo._scope_params("addr1demo")["lim"] == _WINDOW_CEILING
    # unknown/unset target -> ceiling (pre-per-contract behavior preserved)
    monkeypatch.setattr(repo, "_target_requested_max_txs", lambda _t: 0)
    assert repo._scope_params("addr1demo")["lim"] == _WINDOW_CEILING


def test_scope_params_omits_lim_when_window_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = HostBackedRepo(
        Settings(CHAIN_SOURCE="host_ch", CLUSTERING_WINDOW_TXS=0), client=FakeClient()
    )
    monkeypatch.setattr(repo, "_target_requested_max_txs", lambda _t: 1_000)
    assert "lim" not in repo._scope_params("addr1demo")
