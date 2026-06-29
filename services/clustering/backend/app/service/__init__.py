"""Application orchestration shared by the API and the CLI.

Loads features from ClickHouse, runs DBSCAN / evaluation / anomaly detection,
resolves verdicts and builds the graph payload for the UI. Split into focused
submodules — `_common` (shared helpers), `analysis` (on-demand evaluate/cluster/
anomaly), `verdicts` (verdict resolution + read decorators + graph), `online`
(fit/score incremental classification), `pipeline` (the canonical
`process_contract`) — and re-exported here so `from app.service import X` is
unchanged.
"""

from __future__ import annotations

from app.service._common import (
    _FALLBACK_EPS,
    _FALLBACK_MIN_SAMPLES,
    _recommended_params,
    load_clustering_input,
)
from app.service.analysis import (
    cluster_target,
    detect_anomalies_for_target,
    evaluate_target,
)
from app.service.graph import build_graph
from app.service.labels import (
    clear_cluster_members,
    clear_transaction_label,
    label_cluster_members,
    label_transaction,
)
from app.service.online import (
    classify_new_transactions,
    ensure_shape_model,
    update_contract,
)
from app.service.pipeline import process_contract
from app.service.projection import build_projection
from app.service.verdicts import (
    CLUSTER_VERDICTS,
    VERDICT_ANOMALY,
    VERDICT_BENIGN,
    VERDICT_MALICIOUS,
    VERDICT_NORMAL,
    cluster_summary_with_verdicts,
    cluster_transactions_with_verdicts,
    compute_verdicts,
    latest_interactions_with_verdicts,
    top_anomalies_with_verdicts,
)

__all__ = [
    "CLUSTER_VERDICTS",
    "VERDICT_ANOMALY",
    "VERDICT_BENIGN",
    "VERDICT_MALICIOUS",
    "VERDICT_NORMAL",
    "_FALLBACK_EPS",
    "_FALLBACK_MIN_SAMPLES",
    "_recommended_params",
    "build_graph",
    "build_projection",
    "classify_new_transactions",
    "clear_cluster_members",
    "clear_transaction_label",
    "cluster_summary_with_verdicts",
    "cluster_target",
    "cluster_transactions_with_verdicts",
    "compute_verdicts",
    "detect_anomalies_for_target",
    "ensure_shape_model",
    "evaluate_target",
    "label_cluster_members",
    "label_transaction",
    "latest_interactions_with_verdicts",
    "load_clustering_input",
    "process_contract",
    "top_anomalies_with_verdicts",
    "update_contract",
]
