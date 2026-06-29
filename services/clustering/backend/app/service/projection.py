"""The feature-space projection payload for the UI — a verdict-decorated scatter.

A sibling of ``graph.build_graph``: where that view lays transactions out by
address co-occurrence, this one rebuilds the *clustering* feature matrix (the
space DBSCAN actually ran in) and projects it to 2-D/3-D so on-screen proximity
reflects cluster structure. Points are coloured by the same resolved verdicts as
the graph; there are no edges, so the node cap can be higher than the graph's.
"""

from __future__ import annotations

from typing import Any

from app.clustering.projection import project
from app.service._common import load_clustering_input
from app.service.verdicts import VERDICT_NORMAL, _resolve_run, _subset_membership
from app.storage.protocol import Repo


def build_projection(
    repo: Repo,
    run_id: str,
    *,
    dims: int = 2,
    limit: int = 1500,
    cluster: int | None = None,
) -> dict[str, Any]:
    # Clamp to the 2-D/3-D the view supports (the API already enforces this) so a
    # direct caller can't make the echoed `dims` disagree with the projected coords.
    dims = min(max(int(dims), 2), 3)
    ctx = _resolve_run(repo, run_id)

    # Rebuild the exact matrix DBSCAN clustered on, then keep only the txs that are
    # both in this run's membership and present in the (possibly newer) feature set.
    ci = load_clustering_input(repo, ctx.target, ctx.feature_set)
    ci_index = {h: i for i, h in enumerate(ci.tx_hashes)}

    # `total` is the projectable count (∩ with the feature matrix), which for the
    # graph feature set can be below the run's tx_count when the Jaccard build was
    # down-sampled to MAX_GRAPH_TXS — we can only place what we have vectors for.
    subset, total = _subset_membership(ctx.labels, limit=limit, cluster=cluster, keep=ci_index)

    idx = [ci_index[tx] for tx, _ in subset]
    if ci.metric == "precomputed":
        # The MDS branch densely allocates a few O(len(idx)²) matrices; `limit`
        # (≤ MAX_GRAPH_TXS) bounds it to the same order the graph clustering pays.
        data = ci.data[idx][:, idx] if idx else ci.data[:0, :0]
    else:
        data = ci.data[idx] if idx else ci.data[:0]
    coords, axes = project(data, ci.metric, dims, ci.feature_names)

    nodes: list[dict[str, Any]] = []
    for (tx, cid), point in zip(subset, coords, strict=True):
        node = {
            "id": tx,
            "cluster": cid,
            "verdict": ctx.tx_verdict.get(tx, VERDICT_NORMAL),
            "x": float(point[0]),
            "y": float(point[1]),
        }
        if dims >= 3:
            node["z"] = float(point[2])
        nodes.append(node)

    axes_payload = [
        {
            "variance": a.variance,
            "top_features": [{"name": name, "weight": w} for name, w in a.top_features],
        }
        for a in axes
    ]

    return {
        "run_id": run_id,
        "feature_set": ctx.feature_set,
        "dims": dims,
        "metric": ci.metric,
        "axes": axes_payload,
        "nodes": nodes,
        "total": total,
        "shown": len(subset),
        "truncated": total > len(subset),
    }
