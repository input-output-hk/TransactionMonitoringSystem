"""Feature engineering for clustering.

Two feature sets are supported, selected by name:
  * ``shape``    — per-transaction numeric features (Euclidean distance);
  * ``graph``    — address co-occurrence (precomputed Jaccard distance);
  * ``combined`` — shape features + an SVD embedding of the tx/address graph.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.features.graph import build_combined_features, build_jaccard_distance
from app.features.shape import build_shape_features


@dataclass(slots=True)
class ClusteringInput:
    """Uniform input for DBSCAN regardless of feature set."""

    tx_hashes: list[str]
    data: np.ndarray  # feature matrix (euclidean) or distance matrix (precomputed)
    metric: str  # 'euclidean' | 'precomputed'
    feature_set: str
    feature_names: list[str]
    # Transactions the graph down-sample dropped from the window (0 = full
    # coverage); persisted into run notes so readers see a partial population.
    dropped_txs: int = 0


FEATURE_SETS = ("shape", "graph", "combined")


def build_features(
    feature_set: str,
    shape_df: pd.DataFrame | None,
    tx_addresses_df: pd.DataFrame | None,
    *,
    max_graph_txs: int | None = None,
) -> ClusteringInput:
    if feature_set == "shape":
        if shape_df is None:
            raise ValueError("shape feature set requires shape_df")
        tx_hashes, X, names = build_shape_features(shape_df)
        return ClusteringInput(tx_hashes, X, "euclidean", "shape", names)

    if feature_set == "graph":
        if tx_addresses_df is None:
            raise ValueError("graph feature set requires tx_addresses_df")
        tx_hashes, D, dropped = build_jaccard_distance(tx_addresses_df, max_txs=max_graph_txs)
        return ClusteringInput(
            tx_hashes, D, "precomputed", "graph", ["jaccard"], dropped_txs=dropped
        )

    if feature_set == "combined":
        if shape_df is None or tx_addresses_df is None:
            raise ValueError("combined feature set requires both inputs")
        tx_hashes, X, names = build_combined_features(shape_df, tx_addresses_df)
        return ClusteringInput(tx_hashes, X, "euclidean", "combined", names)

    raise ValueError(f"Unknown feature_set {feature_set!r}; expected one of {FEATURE_SETS}")


__all__ = [
    "FEATURE_SETS",
    "ClusteringInput",
    "build_combined_features",
    "build_features",
    "build_jaccard_distance",
    "build_shape_features",
]
