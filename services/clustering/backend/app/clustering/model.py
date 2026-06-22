"""Persisted cluster models + online scoring of new transactions.

This is the online half of the fit/score split (see
docs/online-classification-design.md). A model is fit *once* from a batch DBSCAN
run and frozen; new transactions are then classified against it cheaply —
nearest-centroid assignment plus IsolationForest/LOF *novelty* scores with
per-method vote thresholds — without ever re-running DBSCAN (which has no
predict-for-new-points). Cost is O(clusters) per transaction, independent of
history size.

Only the ``shape`` feature set is supported for now; ``graph`` online scoring
(MinHash signatures over address sets) is a later phase.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import threading
from dataclasses import dataclass, field

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor

from app.anomaly.detect import (
    DEFAULT_TOP_QUANTILE,
    ISO_ESTIMATORS,
    LOF_NEIGHBORS,
)
from app.features.shape import apply_shape_features, fit_shape_features

# Bump when the serialized model layout OR the scoring semantics change: stored
# alongside each model so `ensure_shape_model` discards a stale row and re-fits
# (minting a new model_id), which in turn re-scores the online backlog under the
# new id — without this, a semantics change would only apply to newly-seen txs
# while already-classified rows kept their old, now-wrong `votes`/`verdict`.
# v2: HMAC-signed + zlib-compressed blob format (v1 blobs re-fit lazily).
# v3: online vote semantics — detector-only votes, the collinear cluster-noise
#     flag dropped (see score_shape/_verdict). Forces a re-fit + re-score so
#     upgraded deployments stop showing the pre-v3 false positives.
MODEL_SCHEMA_VERSION = 3

# Verdict strings — mirror the DB enum and service.VERDICT_*; kept local to avoid
# a cycle (service imports this module).
_VERDICT_ANOMALY = "anomaly"
_VERDICT_NORMAL = "normal"

# Generous assignment radius: a new point joins the nearest cluster if it falls
# within this quantile of that cluster's member-to-centroid distances.
_RADIUS_QUANTILE = 0.95


@dataclass(slots=True)
class ShapeModel:
    """Frozen, serializable artifact for online shape-feature classification."""

    feature_names: list[str]
    center: np.ndarray  # RobustScaler center_
    scale: np.ndarray  # RobustScaler scale_
    cluster_ids: list[int]  # non-noise DBSCAN labels, aligned with `centroids`
    centroids: np.ndarray  # (k, d) in scaled space
    radii: np.ndarray  # (k,) assignment radius per cluster
    cluster_verdicts: dict[int, str]  # snapshot of inherited malicious/benign per cluster
    eps: float
    min_samples: int
    iso_threshold: float  # vote threshold (NaN if Isolation Forest not fit)
    lof_threshold: float
    iso_bounds: tuple[float, float]  # (min, max) train scores, for consensus norm
    lof_bounds: tuple[float, float]
    iso_model: IsolationForest | None = None
    lof_model: LocalOutlierFactor | None = None
    n_clusters: int = field(init=False)

    def __post_init__(self) -> None:
        self.n_clusters = len(self.cluster_ids)


@dataclass(slots=True)
class Classification:
    tx_hash: str
    cluster_id: int  # -1 = unassigned (online noise)
    iso_score: float
    lof_score: float
    votes: int
    consensus: float
    verdict: str


def build_shape_model(
    *,
    train_df: pd.DataFrame,
    cluster_of: dict[str, int],
    cluster_verdicts: dict[int, str],
    eps: float,
    min_samples: int,
    top_quantile: float = DEFAULT_TOP_QUANTILE,
    random_state: int = 0,
) -> ShapeModel:
    """Fit a model from a batch run's training set.

    ``train_df`` is the per-tx shape feature frame for the run's members;
    ``cluster_of`` maps tx_hash → DBSCAN label; ``cluster_verdicts`` maps
    cluster_id → inherited verdict (malicious/benign) snapshotted at fit time.
    """
    tx_hashes, X, names, (center, scale) = fit_shape_features(train_df)
    n, d = (X.shape[0], X.shape[1]) if X.ndim == 2 and X.size else (len(tx_hashes), 0)

    labels = np.array([cluster_of.get(h, -1) for h in tx_hashes], dtype=int)
    cluster_ids = sorted(set(labels.tolist()) - {-1})
    centroids: list[np.ndarray] = []
    radii: list[float] = []
    for cid in cluster_ids:
        members = X[labels == cid]
        c = members.mean(axis=0)
        dist = np.linalg.norm(members - c, axis=1)
        centroids.append(c)
        radii.append(float(np.quantile(dist, _RADIUS_QUANTILE)) if dist.size else float(eps))

    iso_model: IsolationForest | None = None
    iso_threshold = float("nan")
    iso_bounds = (float("nan"), float("nan"))
    lof_model: LocalOutlierFactor | None = None
    lof_threshold = float("nan")
    lof_bounds = (float("nan"), float("nan"))

    if n >= 2 and d > 0:
        iso_model = IsolationForest(
            n_estimators=ISO_ESTIMATORS, random_state=random_state, contamination="auto"
        ).fit(X)
        iso_train = -iso_model.score_samples(X)
        iso_threshold = float(np.quantile(iso_train, 1.0 - top_quantile))
        iso_bounds = (float(iso_train.min()), float(iso_train.max()))

        k = max(2, min(LOF_NEIGHBORS, n - 1))
        lof_model = LocalOutlierFactor(n_neighbors=k, novelty=True).fit(X)
        lof_train = -lof_model.score_samples(X)
        lof_threshold = float(np.quantile(lof_train, 1.0 - top_quantile))
        lof_bounds = (float(lof_train.min()), float(lof_train.max()))

    return ShapeModel(
        feature_names=names,
        center=np.asarray(center, dtype=np.float64),
        scale=np.asarray(scale, dtype=np.float64),
        cluster_ids=cluster_ids,
        centroids=np.vstack(centroids) if centroids else np.empty((0, d)),
        radii=np.asarray(radii, dtype=np.float64),
        cluster_verdicts={int(k): v for k, v in cluster_verdicts.items() if v},
        eps=float(eps),
        min_samples=int(min_samples),
        iso_threshold=iso_threshold,
        lof_threshold=lof_threshold,
        iso_bounds=iso_bounds,
        lof_bounds=lof_bounds,
        iso_model=iso_model,
        lof_model=lof_model,
    )


def _norm(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.zeros_like(x)
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


def _verdict(model: ShapeModel, cluster_id: int, votes: int, n_detectors: int) -> str:
    """Effective verdict for a new tx. Precedence mirrors service.compute_verdicts
    (a new tx has no explicit per-tx label): cluster-inherited > auto-anomaly >
    normal.

    Auto-anomaly requires *all available independent novelty detectors to agree*
    (``votes == n_detectors``, both when both IsolationForest and LOF are fit).
    Unlike the batch ensemble (anomaly.detect), the online path does NOT count the
    cluster-noise flag (``cluster_id == -1``) as a vote: batch DBSCAN-noise is
    computed over the whole live population and is genuinely independent, but online
    "noise" (a point outside every frozen cluster's radius) is strongly collinear
    with high iso/lof novelty — counting it would triple-count one underlying
    "far from training" fact and flag any drifted-but-benign traffic. ``cluster_id``
    still carries the noise status, and ``consensus`` still ranks such points higher
    for human review; we only raise the *auto-anomaly* bar."""
    inherited = model.cluster_verdicts.get(cluster_id)
    if inherited:
        return inherited
    auto_anomaly = n_detectors > 0 and votes >= n_detectors
    return _VERDICT_ANOMALY if auto_anomaly else _VERDICT_NORMAL


def score_shape(model: ShapeModel, df: pd.DataFrame) -> list[Classification]:
    """Classify new transactions against a frozen model. ``df`` is their per-tx
    shape feature frame (same columns as the fit input).

    `votes` counts only the *independent* novelty detectors that fired (0..2:
    IsolationForest + LOF) — it does NOT include the cluster-noise flag, which on
    the online path is collinear with them (see `_verdict`). The noise status lives
    in `cluster_id` (-1 = unassigned). `consensus` is normalized against the model's
    *training* score bounds — points more extreme than anything in training clip to
    1.0 — and still folds in the noise signal so unassigned points rank higher for
    review; it is NOT on the same scale as `anomaly_scores.consensus` (rank-normalized
    over the live population), so don't compare them. `votes` (detector agreement) is
    the verdict-driving signal."""
    tx_hashes, X, _ = apply_shape_features(df, model.center, model.scale)
    n = len(tx_hashes)
    if n == 0:
        return []

    cids = np.full(n, -1, dtype=int)
    if model.centroids.shape[0]:
        dist = np.linalg.norm(X[:, None, :] - model.centroids[None, :, :], axis=2)  # (n,k)
        nearest = np.argmin(dist, axis=1)
        nearest_d = dist[np.arange(n), nearest]
        within = nearest_d <= model.radii[nearest]
        assigned = np.asarray(model.cluster_ids)[nearest]
        cids = np.where(within, assigned, -1)

    if model.iso_model is not None:
        iso = -model.iso_model.score_samples(X)
    else:
        iso = np.full(n, np.nan)
    lof = (
        -model.lof_model.score_samples(X)
        if model.lof_model is not None
        else np.full(n, np.nan)
    )

    # Vote only on the independent novelty detectors; the cluster-noise flag is
    # deliberately excluded (collinear with iso/lof — see `_verdict`).
    #
    # INVARIANT: build_shape_model fits IsolationForest and LOF together, so
    # n_detectors is always 0 or 2. The Latest-feed re-derives the online verdict
    # as `votes >= FLAG_VOTE_THRESHOLD` (== 2) in service.compute_verdicts; that
    # coincides with the `votes >= n_detectors` rule below only while n_detectors
    # == FLAG_VOTE_THRESHOLD. If a single-detector online model is ever introduced,
    # update both sites together or the stored and displayed verdicts will diverge.
    votes = np.zeros(n, dtype=int)
    n_detectors = 0
    if model.iso_model is not None:
        votes = votes + (iso >= model.iso_threshold).astype(int)
        n_detectors += 1
    if model.lof_model is not None:
        votes = votes + (lof >= model.lof_threshold).astype(int)
        n_detectors += 1

    signals = [(cids == -1).astype(float)]
    if model.lof_model is not None:
        signals.append(_norm(lof, *model.lof_bounds))
    if model.iso_model is not None:
        signals.append(_norm(iso, *model.iso_bounds))
    consensus = np.mean(np.vstack(signals), axis=0)

    return [
        Classification(
            tx_hash=tx_hashes[i],
            cluster_id=int(cids[i]),
            iso_score=float(iso[i]),
            lof_score=float(lof[i]),
            votes=int(votes[i]),
            consensus=float(consensus[i]),
            verdict=_verdict(model, int(cids[i]), int(votes[i]), n_detectors),
        )
        for i in range(n)
    ]


class ModelIntegrityError(RuntimeError):
    """The stored model blob failed authentication (or predates signing).

    Raised BEFORE any deserialization: ``joblib.load`` is pickle underneath, so a
    tampered blob would otherwise be remote code execution. There is deliberately
    no legacy fallback — an unsigned blob is treated as untrusted, and the lazy
    rebuild path (``ensure_shape_model`` discards stale ``schema_version`` rows)
    re-fits and re-signs from the canonical run on next use.
    """


# Blob layout: "tms-model:1:<hex hmac-sha256 of payload>:<base64 payload>".
# The version prefix means a later move of the payload bytes to disk/object
# storage only changes residency, not the verification scheme.
_BLOB_PREFIX = "tms-model"
_BLOB_VERSION = "1"
_UNSIGNED_DIGEST = "unsigned"


def _signing_keys() -> list[bytes]:
    """Configured signing keys: sign with the first, verify against any (rotation).
    Empty when MODEL_SIGNING_KEYS is unset → blobs are stored/loaded unsigned, for
    zero-config local demos (mirrors the API_KEY pattern); production must set it."""
    from app.config import get_settings  # late: avoid import cycles at module load

    raw = get_settings().model_signing_keys
    return [k.strip().encode() for k in raw.split(",") if k.strip()]


def _digest(payload: bytes, key: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def serialize_model(model: ShapeModel) -> str:
    """Serialize + HMAC-sign a model for storage in a ClickHouse String.

    Compressed (the 300-tree IsolationForest pickles ~3-5x smaller) and signed
    with the first configured key; unsigned when no key is configured."""
    buf = io.BytesIO()
    joblib.dump(model, buf, compress=("zlib", 3))
    payload = buf.getvalue()
    keys = _signing_keys()
    digest = _digest(payload, keys[0]) if keys else _UNSIGNED_DIGEST
    return f"{_BLOB_PREFIX}:{_BLOB_VERSION}:{digest}:" + base64.b64encode(payload).decode("ascii")


def deserialize_model(blob: str) -> ShapeModel:
    """Verify, then load a stored model blob.

    Raises ``ModelIntegrityError`` when the blob predates the signed format, is
    signed but no/none-of-the configured keys match, or is unsigned while keys are
    configured. Verification happens before ``joblib.load`` touches the payload."""
    parts = blob.split(":", 3)
    if len(parts) != 4 or parts[0] != _BLOB_PREFIX or parts[1] != _BLOB_VERSION:
        raise ModelIntegrityError(
            "model blob predates the signed format; it will be re-fit on next use"
        )
    digest, b64 = parts[2], parts[3]
    payload = base64.b64decode(b64)
    keys = _signing_keys()
    if keys:
        if not any(hmac.compare_digest(digest, _digest(payload, k)) for k in keys):
            raise ModelIntegrityError("model blob failed HMAC verification")
    elif digest != _UNSIGNED_DIGEST:
        raise ModelIntegrityError(
            "model blob is signed but MODEL_SIGNING_KEYS is not configured"
        )
    return joblib.load(io.BytesIO(payload))


# Small LRU of deserialized models keyed by model_id. A model_id is unique per fit
# (uuid) and its blob is immutable, so a cached entry can never go stale — a re-fit
# mints a new id. This spares repeated reads (anomaly-reason attribution runs on every
# Latest/Outliers load) from re-unpickling the multi-MB IsolationForest/LOF just to
# read center/scale. Models are read-only after load, so sharing one instance is safe.
_MODEL_CACHE: dict[str, ShapeModel] = {}
_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE_MAX = 8


def load_cluster_model(model_row: dict) -> ShapeModel:
    """``deserialize_model`` with an LRU cache keyed by ``model_row["model_id"]``."""
    model_id = model_row["model_id"]
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(model_id)
    if cached is not None:
        return cached
    model = deserialize_model(model_row["blob"])  # outside the lock: slow + pure
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE[model_id] = model
        while len(_MODEL_CACHE) > _MODEL_CACHE_MAX:
            _MODEL_CACHE.pop(next(iter(_MODEL_CACHE)))  # evict oldest (insertion order)
    return model
