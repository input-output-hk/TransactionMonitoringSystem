"""The co-spend graph payload for the UI — a verdict-decorated view.

Composes the run's membership, the verdict resolution from ``verdicts`` and the
Jaccard edge builder from ``features.graph`` into the nodes/edges payload. A
sibling of the read decorators in ``verdicts``, split out because graph
subsetting/capping is its own concern.
"""

from __future__ import annotations

from typing import Any

from app.features.graph import build_graph_edges
from app.service.verdicts import VERDICT_NORMAL, _resolve_run, _subset_membership
from app.storage.protocol import Repo


def build_graph(
    repo: Repo,
    run_id: str,
    *,
    limit: int = 400,
    cluster: int | None = None,
    max_edges: int = 2500,
) -> dict[str, Any]:
    ctx = _resolve_run(repo, run_id)
    subset, total = _subset_membership(ctx.labels, limit=limit, cluster=cluster)
    subset_hashes = [tx for tx, _ in subset]

    addr_df = repo.fetch_addresses_for_txs(ctx.target, subset_hashes)
    edges = build_graph_edges(addr_df, subset_hashes, max_edges=max_edges)

    nodes = [
        {"id": tx, "cluster": cid, "verdict": ctx.tx_verdict.get(tx, VERDICT_NORMAL)}
        for tx, cid in subset
    ]
    edge_payload = [{"source": s, "target": t, "weight": w} for (s, t, w) in edges]
    return {
        "run_id": run_id,
        "nodes": nodes,
        "edges": edge_payload,
        "total": total,
        "shown": len(subset),
        "truncated": total > len(subset),
    }
