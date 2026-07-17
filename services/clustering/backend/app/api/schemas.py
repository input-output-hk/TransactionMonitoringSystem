"""Pydantic request/response models for the API."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, computed_field

from app.anomaly.detect import DEFAULT_TOP_QUANTILE
from app.config import get_settings

# Matches an explicit timezone suffix: Z or a +HH:MM / -HHMM style offset.
_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def _iso_z(v: str) -> str:
    """Normalize a ClickHouse ``YYYY-MM-DD HH:MM:SS`` string to Z-suffixed ISO.

    Storage stringifies timestamps server-side (``toString(...)``), which
    yields a space separator and no timezone marker; the values are UTC by the
    schema's convention. The wire contract (shared with the host API) is
    ``YYYY-MM-DDTHH:MM:SSZ``, so replace the first space with ``T`` and append
    ``Z`` unless an explicit suffix is already present (pass-through guard:
    re-validation must not double-suffix).
    """
    if not v:
        return v
    v = v.replace(" ", "T", 1)
    if _TZ_SUFFIX_RE.search(v):
        return v
    return v + "Z"


# Wire timestamp type: every response-model timestamp field uses this so the
# sidecar emits the same Z-suffixed UTC format as the host API.
UtcIsoStr = Annotated[str, AfterValidator(_iso_z)]


class ListPage[ItemT](BaseModel):
    """Shared list envelope matching the host API's ListResponse contract."""

    count: int
    total: int
    data: list[ItemT]


# Pagination bounds shared by every collection endpoint; they mirror the host
# API's ListResponse contract (default page of 100, hard cap of 1000 per
# request) so the two surfaces page identically.
LIST_LIMIT_DEFAULT = 100
LIST_LIMIT_MAX = 1000


FeatureSet = Literal["shape", "graph", "combined"]

# Effective per-tx verdict surfaced on cluster/graph views.
Verdict = Literal["malicious", "benign", "anomaly", "normal"]
# Verdict a user may explicitly apply to a whole cluster.
ClusterVerdict = Literal["malicious", "benign"]

# Length caps for user-supplied free text: a cluster/tx note and a contract
# display label.
MAX_NOTE_LEN = 240
MAX_LABEL_LEN = 120


class ClusterLabelRequest(BaseModel):
    verdict: ClusterVerdict
    note: str = Field(default="", max_length=MAX_NOTE_LEN)


class TxLabelRequest(BaseModel):
    verdict: ClusterVerdict
    note: str = Field(default="", max_length=MAX_NOTE_LEN)


class ClusterRequest(BaseModel):
    target: str = Field(min_length=1)
    feature_set: FeatureSet = "shape"
    eps: float = Field(gt=0)
    min_samples: int = Field(ge=2)
    notes: str = ""


class AnomalyRequest(BaseModel):
    target: str = Field(min_length=1)
    feature_set: FeatureSet = "shape"
    eps: float | None = Field(default=None, gt=0)
    min_samples: int | None = Field(default=None, ge=2)
    top_quantile: float = Field(default=DEFAULT_TOP_QUANTILE, gt=0, lt=1)


# Upper bound on a single onboarding request's download, so one call can't kick
# off an unbounded ingest (paid-quota / DoS guard).
MAX_TXS_CAP = 50_000


class ContractRequest(BaseModel):
    target: str = Field(min_length=1)
    max_txs: int | None = Field(default=None, ge=1, le=MAX_TXS_CAP)
    reprocess: bool = False
    # Optional user-supplied display name; takes precedence over the registry
    # label and persists across reprocess. Empty → fall back to the registry.
    label: str = Field(default="", max_length=MAX_LABEL_LEN)


class RenameRequest(BaseModel):
    label: str = Field(default="", max_length=MAX_LABEL_LEN)


class TargetOut(BaseModel):
    target: str
    target_type: str
    tx_count: int


class RunOut(BaseModel):
    run_id: str
    target: str
    feature_set: str
    eps: float
    min_samples: int
    metric: str
    n_points: int
    n_clusters: int
    n_noise: int
    silhouette: float | None
    origin: str
    created_at: UtcIsoStr


class ClusterSummaryOut(BaseModel):
    cluster_id: int
    size: int
    avg_fees: float
    avg_output_lovelace: float
    avg_inputs: float
    avg_outputs: float
    avg_assets: float
    # Manual verdict applied to this cluster (None = unlabeled), whether its explicit
    # member labels disagree, how many members are explicitly labeled, and how many
    # are auto-flagged anomalies (votes >= 2).
    verdict: ClusterVerdict | None = None
    verdict_conflict: bool = False
    labeled_count: int = 0
    anomaly_count: int = 0


# --- Response models -------------------------------------------------------------
# Every endpoint declares one of these, so /openapi.json is a complete, typed
# contract for external UIs (the bundled SPA's types mirror them in ui/src/types.ts).


class HealthOut(BaseModel):
    status: str


class ReadyOut(BaseModel):
    status: str


class ConfigOut(BaseModel):
    # Read-only deployment facts the UI needs to shape its onboarding form.
    # host_backed: the engine reads each contract's txs from the host tables, so
    # there is no per-contract download to cap — fits run over the rolling
    # window of size window_txs instead. A per-contract "max txs" is meaningless
    # in that mode, so the form hides it.
    # history_source: the deployment's secondary pre-deployment-history source
    # ("" when disabled). When set, the form re-exposes "max txs" as the
    # per-contract history depth.
    host_backed: bool
    window_txs: int
    history_source: str = ""


class IdentifyOut(BaseModel):
    valid: bool
    target_type: str | None
    script_hash: str | None
    label: str


class ContractOut(BaseModel):
    target: str
    target_type: str
    label: str
    # DB column is `present` (avoids the EXISTS keyword); the API field is `exists`.
    exists: int
    is_script: int
    script_type: str
    balance_lovelace: int
    asset_count: int
    sample_tokens: str  # JSON-encoded [{unit, policy_id, name}]
    status: str
    requested_max_txs: int
    updated_at: UtcIsoStr
    tx_count: int
    # Trailing online-noise rate written by the incremental classifier; 0 until a
    # classify run has scored against this contract's model.
    drift_score: float = 0.0
    # Pre-deployment history backfill visibility (HISTORY_SOURCE deployments).
    # Both are derived at read time on the DETAIL endpoint only (0/"none" in
    # list views): the count is the locally-stored history subset, the status
    # comes from the backfill's cursor marker — "none" (never marked, or the
    # feature is disabled), "in_progress", "complete".
    history_tx_count: int = 0
    history_status: str = "none"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reclustering_suggested(self) -> bool:
        """True when recent traffic drifted enough from the frozen model that a
        full re-cluster is warranted (drift_score over RECLUSTER_NOISE_THRESHOLD).
        Derived at read time so the threshold can be retuned without re-running."""
        return get_settings().recluster_recommended(self.drift_score)


class JobOut(BaseModel):
    job_id: str
    target: str
    target_type: str
    max_txs: int
    reprocess: int
    kind: str  # 'onboard' | 'classify'
    status: str
    stage_detail: str
    txs_done: int
    error: str
    created_at: UtcIsoStr
    updated_at: UtcIsoStr


class JobAck(BaseModel):
    """POST /contracts — the enqueued onboarding job."""

    job_id: str
    target: str
    target_type: str


class ClassifyJobAck(BaseModel):
    """POST /contracts/{target}/classify-new — the enqueued classify job."""

    job_id: str
    target: str
    kind: str


class ContractDeleteAck(BaseModel):
    deleted: bool
    target: str


class AnomalyRunDeleteAck(BaseModel):
    deleted: bool
    run_id: str


class ClusterTxOut(BaseModel):
    tx_hash: str
    block_time: UtcIsoStr
    fees: int
    total_output_lovelace: int
    input_count: int
    output_count: int
    distinct_assets: int
    redeemer_count: int
    # Effective verdict (precedence: own label > cluster label > anomaly > normal)
    # and the tx's OWN explicit label (None = inherited/auto only).
    verdict: Verdict
    label: ClusterVerdict | None
    votes: int


class ClusterTxPage(BaseModel):
    run_id: str
    cluster_id: int
    transactions: list[ClusterTxOut]


class ClusterLabelAck(BaseModel):
    run_id: str
    cluster_id: int
    verdict: ClusterVerdict
    labeled: int


class ClusterClearAck(BaseModel):
    run_id: str
    cluster_id: int
    cleared: int


class TxLabelAck(BaseModel):
    target: str
    tx_hash: str
    verdict: ClusterVerdict
    labeled: int


class TxClearAck(BaseModel):
    target: str
    tx_hash: str
    cleared: int


class GraphNodeOut(BaseModel):
    id: str
    cluster: int
    verdict: Verdict


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    weight: float


class GraphOut(BaseModel):
    run_id: str
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    total: int
    shown: int
    truncated: bool


class ProjectionNodeOut(BaseModel):
    id: str
    cluster: int
    verdict: Verdict
    x: float
    y: float
    z: float | None = None


class AxisFeatureOut(BaseModel):
    name: str
    weight: float


class ProjectionAxisOut(BaseModel):
    # Fraction of variance this axis explains (PCA); None for MDS axes.
    variance: float | None = None
    # Features driving this axis, largest |loading| first (empty for MDS).
    top_features: list[AxisFeatureOut] = []


class ProjectionOut(BaseModel):
    run_id: str
    feature_set: str
    dims: int
    metric: str
    axes: list[ProjectionAxisOut]
    nodes: list[ProjectionNodeOut]
    total: int
    shown: int
    truncated: bool


class KDistanceOut(BaseModel):
    k: int
    distances: list[float]
    knee_eps: float | None


class GridPointOut(BaseModel):
    eps: float
    min_samples: int
    n_clusters: int
    n_noise: int
    noise_ratio: float
    silhouette: float | None


class RecommendedOut(BaseModel):
    eps: float
    min_samples: int
    rationale: str


class EvaluationOut(BaseModel):
    feature_set: str
    metric: str
    n_points: int
    n_features: int | None
    k_distance: KDistanceOut
    grid: list[GridPointOut]
    recommended: RecommendedOut | None
    message: str | None = None


class ClusterRunAck(BaseModel):
    """POST /cluster — the persisted custom run's summary."""

    run_id: str
    target: str
    feature_set: str
    eps: float
    min_samples: int
    n_points: int
    n_clusters: int
    n_noise: int
    silhouette: float | None
    origin: str


class AnomalyDetectAck(BaseModel):
    """POST /anomaly — the persisted run's summary (methods as a list here;
    the stored run row serialises them comma-joined, see AnomalyRunOut)."""

    run_id: str
    target: str
    feature_set: str
    methods: list[str]
    n_points: int
    n_flagged: int
    eps: float
    min_samples: int


class AnomalyRunOut(BaseModel):
    run_id: str
    target: str
    feature_set: str
    methods: str  # comma-joined detector names
    n_points: int
    n_flagged: int
    eps: float
    min_samples: int
    top_quantile: float
    origin: str
    created_at: UtcIsoStr


class AnomalyReason(BaseModel):
    """One human-readable driver of an anomaly verdict (top deviating shape feature)."""

    label: str  # "inputs", "output value", "fee", "time of day", "unusual combination"
    direction: str  # "high" | "low" | "unusual" | "combo"
    detail: str  # "far above typical", "unusual time of day", …


class AnomalyCandidateOut(BaseModel):
    score_rank: int
    tx_hash: str
    consensus: float
    votes: int
    iso_score: float | None  # None on graph runs (no feature vectors)
    lof_score: float
    dbscan_noise: int
    block_time: UtcIsoStr
    fees: int
    size: int
    total_input_lovelace: int
    total_output_lovelace: int
    net_lovelace: int
    input_count: int
    output_count: int
    distinct_assets: int
    redeemer_count: int
    hour_of_day: int
    day_of_week: int
    verdict: Verdict
    label: ClusterVerdict | None
    reasons: list[AnomalyReason] = []  # populated only for shape anomaly rows


class AnomalyTopPage(BaseModel):
    run_id: str
    run: AnomalyRunOut
    candidates: list[AnomalyCandidateOut]


class LatestInteractionOut(BaseModel):
    tx_hash: str
    block_time: UtcIsoStr
    fees: int
    size: int
    total_input_lovelace: int
    total_output_lovelace: int
    net_lovelace: int
    input_count: int
    output_count: int
    distinct_assets: int
    redeemer_count: int
    # `classified` is False for a tx that's in no cluster run, isn't online-scored
    # against the current run's model, and has no explicit label; verdict/cluster are
    # then unknown (None). An explicit per-tx label always classifies and wins.
    classified: bool
    verdict: Verdict | None
    label: ClusterVerdict | None
    cluster_id: int | None
    votes: int
    reasons: list[AnomalyReason] = []  # populated only for shape anomaly rows


class LatestInteractionsPage(BaseModel):
    target: str
    feature_set: str
    transactions: list[LatestInteractionOut]
