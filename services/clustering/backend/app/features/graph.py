"""Address co-occurrence features.

Each transaction is represented by the set of addresses appearing in its UTXOs.
The pairwise distance between two transactions is the Jaccard distance of their
address sets (``1 - intersection / union``), computed efficiently from a sparse
tx-by-address incidence matrix. The combined feature set augments the numeric
shape features with a low-dimensional SVD embedding of the same incidence.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.decomposition import TruncatedSVD
from sklearn.preprocessing import StandardScaler

from app import tunables
from app.features.shape import build_shape_features
from app.registry.bech32 import stake_credential_hex

logger = logging.getLogger(__name__)

# Value lives in config/clustering.yaml (section `graph`), validated at import
# by app.tunables; the constant name is unchanged.
_SVD_COMPONENTS: int = int(tunables.get("graph")["svd_components"])


@lru_cache(maxsize=200_000)
def entity_key(address: str) -> str:
    """Map an address to the entity (wallet) that controls it.

    On Cardano one wallet = one stake credential controlling many payment
    addresses, so grouping by the raw payment address makes the same wallet look
    like distinct entities (it constantly rotates change/one-time addresses). We
    therefore key co-occurrence on the **stake credential** when derivable, and
    fall back to the raw address for enterprise/pointer/Byron/script addresses
    that have no stake part. Cached because addresses repeat heavily across UTXOs.
    """
    sk = stake_credential_hex(address)
    return f"stake:{sk}" if sk else address


def _build_incidence(
    df: pd.DataFrame, tx_order: list[str] | None = None
) -> tuple[list[str], sparse.csr_matrix]:
    """Build a binary tx-by-address incidence matrix.

    If ``tx_order`` is given, rows follow that order (addresses for unknown txs
    are ignored); otherwise rows are the sorted unique tx hashes in ``df``.

    A no-rows result is normal (a target whose txs carry no co-spend address, or
    the empty-result frame ClickHouse's ``query_df`` returns with no columns at
    all): build an empty incidence over ``tx_order`` rather than letting
    ``astype`` raise KeyError on the absent columns.
    """
    if df.empty:
        tx_hashes = list(tx_order) if tx_order is not None else []
        return tx_hashes, sparse.csr_matrix((len(tx_hashes), 0), dtype=np.float64)
    df = df.astype({"tx_hash": str, "address": str})
    if tx_order is None:
        tx_hashes = sorted(df["tx_hash"].unique().tolist())
    else:
        tx_hashes = list(tx_order)
    tx_index = {h: i for i, h in enumerate(tx_hashes)}

    # Resolve each address to its controlling entity (stake key when derivable)
    # so co-occurrence is measured per wallet, not per rotating payment address.
    entities = [entity_key(a) for a in df["address"]]
    addr_index = {a: j for j, a in enumerate(dict.fromkeys(entities))}

    rows: list[int] = []
    cols: list[int] = []
    for tx_hash, entity in zip(df["tx_hash"], entities, strict=True):
        i = tx_index.get(tx_hash)
        if i is None:
            continue
        rows.append(i)
        cols.append(addr_index[entity])

    data = np.ones(len(rows), dtype=np.float64)
    matrix = sparse.csr_matrix(
        (data, (rows, cols)), shape=(len(tx_hashes), len(addr_index)), dtype=np.float64
    )
    # Collapse any accidental duplicates to binary.
    matrix.data[:] = 1.0
    return tx_hashes, matrix


def _recency_order(df: pd.DataFrame) -> list[str]:
    """Unique tx hashes ordered newest ``block_time`` first, with an ascending
    hash tiebreak so equal-timestamp cuts stay deterministic across runs."""
    latest = df.groupby("tx_hash")["block_time"].max().reset_index()
    latest = latest.sort_values(["block_time", "tx_hash"], ascending=[False, True])
    return latest["tx_hash"].astype(str).tolist()


def build_jaccard_distance(
    df: pd.DataFrame, max_txs: int | None = None
) -> tuple[list[str], np.ndarray, int]:
    """Return ``(tx_hashes, D, n_dropped)`` where ``D`` is an n-by-n Jaccard
    distance matrix.

    The distance matrix is dense (n*n), so when the transaction count exceeds
    ``max_txs`` the set is sampled down to bound memory. The sample keeps the
    most RECENT transactions (by the ``block_time`` column when the frame
    carries one): current activity is what an operator is investigating, so the
    droppable part is the old history, not an arbitrary hash-ordered slice of
    the window. Frames without ``block_time`` fall back to the deterministic
    hash order. ``n_dropped`` is returned so callers can surface the drop in
    run metadata rather than only in the log.
    """
    n_dropped = 0
    # The empty guard also covers the column-less frame ClickHouse's query_df
    # returns for a zero-row result, which astype would reject with a KeyError.
    if max_txs is not None and not df.empty:
        df = df.astype({"tx_hash": str})
        unique = sorted(df["tx_hash"].unique().tolist())
        if len(unique) > max_txs:
            n_dropped = len(unique) - max_txs
            ordered = _recency_order(df) if "block_time" in df.columns else unique
            keep = set(ordered[:max_txs])
            logger.warning(
                "graph clustering capped at %d of %d transactions (%d oldest "
                "dropped; recorded in run notes); raise MAX_GRAPH_TXS or use "
                "the 'combined' feature set for full coverage.",
                max_txs,
                len(unique),
                n_dropped,
            )
            df = df[df["tx_hash"].isin(keep)]

    tx_hashes, matrix = _build_incidence(df)
    n = len(tx_hashes)
    if n == 0:
        return [], np.empty((0, 0)), n_dropped

    sizes = np.asarray(matrix.sum(axis=1)).ravel()
    inter = (matrix @ matrix.T).toarray()
    union = sizes[:, None] + sizes[None, :] - inter
    with np.errstate(divide="ignore", invalid="ignore"):
        sim = np.where(union > 0, inter / union, 0.0)
    distance = 1.0 - sim
    np.fill_diagonal(distance, 0.0)
    return tx_hashes, np.clip(distance, 0.0, 1.0), n_dropped


def build_combined_features(
    shape_df: pd.DataFrame, tx_addresses_df: pd.DataFrame
) -> tuple[list[str], np.ndarray, list[str]]:
    """Concatenate scaled shape features with an SVD embedding of the tx graph."""
    tx_hashes, X_shape, names = build_shape_features(shape_df)
    if not tx_hashes:
        return [], np.empty((0, 0)), []

    _, matrix = _build_incidence(tx_addresses_df, tx_order=tx_hashes)
    # TruncatedSVD requires n_components < n_features (and <= n_samples).
    k = min(_SVD_COMPONENTS, matrix.shape[1] - 1, matrix.shape[0] - 1)
    if k < 1:
        return tx_hashes, X_shape, names

    embedding = TruncatedSVD(n_components=k, random_state=0).fit_transform(matrix)
    embedding = StandardScaler().fit_transform(embedding)
    X = np.hstack([X_shape, embedding])
    names = [*names, *[f"svd_{i}" for i in range(k)]]
    return tx_hashes, X.astype(np.float64), names


def build_graph_edges(
    df: pd.DataFrame, tx_subset: list[str], max_edges: int = 2000
) -> list[tuple[str, str, int]]:
    """Edges between transactions in ``tx_subset`` that share ≥1 address.

    Weight is the number of shared addresses. Returns up to ``max_edges`` of the
    most strongly connected pairs.
    """
    if len(tx_subset) < 2:
        return []
    tx_hashes, matrix = _build_incidence(df, tx_order=tx_subset)
    inter = (matrix @ matrix.T).toarray()
    iu = np.triu_indices(len(tx_hashes), k=1)
    weights = inter[iu]
    nonzero = np.nonzero(weights)[0]
    order = nonzero[np.argsort(weights[nonzero])[::-1]][:max_edges]
    return [(tx_hashes[int(iu[0][i])], tx_hashes[int(iu[1][i])], int(weights[i])) for i in order]
