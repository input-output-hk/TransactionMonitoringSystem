"""Tests for feature builders (shape, Jaccard graph distance, edges)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.features.graph import (
    build_combined_features,
    build_graph_edges,
    build_jaccard_distance,
)
from app.features.shape import build_shape_features


def _shape_df(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "tx_hash": [f"tx{i}" for i in range(n)],
            "fees": [200_000 + i for i in range(n)],
            "size": [400 + i for i in range(n)],
            "input_count": [1 + i % 3 for i in range(n)],
            "output_count": [2 + i % 2 for i in range(n)],
            "total_input_lovelace": [5_000_000 * (i + 1) for i in range(n)],
            "total_output_lovelace": [4_800_000 * (i + 1) for i in range(n)],
            "net_lovelace": [-200_000 for _ in range(n)],
            "distinct_assets": [i % 4 for i in range(n)],
            "redeemer_count": [i % 2 for i in range(n)],
            "hour_of_day": [i % 24 for i in range(n)],
            "day_of_week": [1 + i % 7 for i in range(n)],
        }
    )


def test_build_shape_features_shape_and_names() -> None:
    df = _shape_df(6)
    tx_hashes, X, names = build_shape_features(df)
    assert tx_hashes == [f"tx{i}" for i in range(6)]
    assert X.shape == (6, len(names))
    assert {"hour_sin", "hour_cos", "dow_sin", "dow_cos"} <= set(names)
    assert np.isfinite(X).all()


def test_build_shape_features_empty() -> None:
    tx_hashes, _X, names = build_shape_features(_shape_df(0))
    assert tx_hashes == []
    assert names == []


def test_jaccard_distance() -> None:
    df = pd.DataFrame(
        {
            "tx_hash": ["t1", "t1", "t2", "t2", "t3"],
            "address": ["a", "b", "b", "c", "x"],
        }
    )
    tx_hashes, D, dropped = build_jaccard_distance(df)
    idx = {h: i for i, h in enumerate(tx_hashes)}
    assert dropped == 0
    # t1={a,b}, t2={b,c} -> jaccard sim 1/3 -> distance 2/3
    assert D[idx["t1"], idx["t2"]] == pytest.approx(2 / 3)
    # t1 vs t3 share nothing -> distance 1
    assert D[idx["t1"], idx["t3"]] == pytest.approx(1.0)
    # symmetric, zero diagonal
    assert np.allclose(D, D.T)
    assert np.allclose(np.diag(D), 0.0)


def test_jaccard_distance_caps_txs() -> None:
    # Without a block_time column the cap falls back to deterministic hash order.
    df = pd.DataFrame(
        {
            "tx_hash": ["t1", "t2", "t3", "t4"],
            "address": ["a", "b", "c", "d"],
        }
    )
    tx_hashes, D, dropped = build_jaccard_distance(df, max_txs=2)
    assert tx_hashes == ["t1", "t2"]
    assert D.shape == (2, 2)
    assert dropped == 2


def test_jaccard_distance_caps_by_recency_when_block_time_present() -> None:
    # With block_time in the frame, the cap keeps the NEWEST transactions: the
    # current activity an operator is investigating, not a hash-ordered slice.
    df = pd.DataFrame(
        {
            "tx_hash": ["t1", "t2", "t3", "t4"],
            "address": ["a", "b", "c", "d"],
            "block_time": pd.to_datetime(
                ["2026-01-01", "2026-01-04", "2026-01-02", "2026-01-03"]
            ),
        }
    )
    tx_hashes, _D, dropped = build_jaccard_distance(df, max_txs=2)
    assert set(tx_hashes) == {"t2", "t4"}  # the two newest
    assert dropped == 2


def test_jaccard_distance_recency_tiebreak_is_deterministic() -> None:
    # Equal timestamps: the hash tiebreak must make the cut reproducible.
    df = pd.DataFrame(
        {
            "tx_hash": ["t3", "t1", "t2"],
            "address": ["a", "b", "c"],
            "block_time": pd.to_datetime(["2026-01-01"] * 3),
        }
    )
    first, _, _ = build_jaccard_distance(df, max_txs=2)
    second, _, _ = build_jaccard_distance(df, max_txs=2)
    assert first == second == ["t1", "t2"]


def test_jaccard_distance_empty_frame_with_cap() -> None:
    # The column-less zero-row frame ClickHouse's query_df returns must not
    # crash the cap's astype (production always passes max_txs).
    tx_hashes, D, dropped = build_jaccard_distance(pd.DataFrame(), max_txs=5)
    assert tx_hashes == [] and D.shape == (0, 0) and dropped == 0


def test_combined_single_address_falls_back_to_shape() -> None:
    shape_df = _shape_df(5)
    # Every transaction touches the same single address -> 1-column incidence,
    # so the SVD step must be skipped (no svd_* features).
    addr_df = pd.DataFrame({"tx_hash": [f"tx{i}" for i in range(5)], "address": ["a"] * 5})
    tx_hashes, X, names = build_combined_features(shape_df, addr_df)
    assert len(tx_hashes) == 5
    assert not any(n.startswith("svd_") for n in names)
    assert X.shape == (5, len(names))


def test_graph_edges() -> None:
    df = pd.DataFrame(
        {
            "tx_hash": ["t1", "t1", "t2", "t2", "t3"],
            "address": ["a", "b", "b", "c", "x"],
        }
    )
    edges = build_graph_edges(df, ["t1", "t2", "t3"])
    pairs = {(min(s, t), max(s, t)): w for (s, t, w) in edges}
    assert pairs == {("t1", "t2"): 1}


def test_graph_edges_empty_address_frame() -> None:
    """A column-less empty frame (what ClickHouse's query_df returns for a
    zero-row result) must yield no edges, not a KeyError 500 from astype."""
    edges = build_graph_edges(pd.DataFrame(), ["t1", "t2", "t3"])
    assert edges == []


def test_jaccard_distance_empty_address_frame() -> None:
    """Same column-less empty frame on the clustering path: no rows, no crash."""
    tx_hashes, D, _dropped = build_jaccard_distance(pd.DataFrame())
    assert tx_hashes == [] and D.shape == (0, 0)
