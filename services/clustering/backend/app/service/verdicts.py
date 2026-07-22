"""Verdict resolution + the read-path decorators that surface it.

The pure core (``compute_verdicts``: manual label > cluster inheritance >
auto-anomaly) plus the repo-backed resolution helpers and the four
verdict-decorated reads (cluster summary/transactions, online classifications,
top anomalies). Label WRITES live in ``labels``; the graph view in ``graph``.
"""

from __future__ import annotations

import logging
from collections.abc import Container
from dataclasses import asdict, dataclass
from typing import Any

from app.anomaly.detect import FLAG_VOTE_THRESHOLD
from app.clustering.model import MODEL_SCHEMA_VERSION, ModelIntegrityError, load_cluster_model
from app.features.explain import explain_shape_deviations
from app.storage.protocol import Repo

logger = logging.getLogger(__name__)

# Effective per-tx verdicts surfaced on cluster/graph views.
VERDICT_MALICIOUS = "malicious"
VERDICT_BENIGN = "benign"
VERDICT_ANOMALY = "anomaly"
VERDICT_NORMAL = "normal"
# Verdicts a user may explicitly apply to a cluster.
CLUSTER_VERDICTS = (VERDICT_MALICIOUS, VERDICT_BENIGN)


def _dominant_verdict(applied: Container[str]) -> str | None:
    """Reduce a set of applied cluster labels to the single inheritable verdict,
    malicious winning on conflict (malicious > benign > none). The one place this
    precedence is expressed, shared by the cluster-inheritance resolution."""
    if VERDICT_MALICIOUS in applied:
        return VERDICT_MALICIOUS
    if VERDICT_BENIGN in applied:
        return VERDICT_BENIGN
    return None


def compute_verdicts(
    cluster_of: dict[str, int],
    explicit: dict[str, str],
    votes: dict[str, int],
    *,
    anomaly_threshold: int = FLAG_VOTE_THRESHOLD,
    propagating: set[str] | None = None,
) -> tuple[dict[str, str], dict[int, dict[str, Any]]]:
    """Resolve the effective verdict for every tx in a run and per cluster.

    Single source of truth for the precedence (highest wins): explicit per-tx label
    > cluster-inherited label > auto-anomaly (``votes >= anomaly_threshold``).

    Inheritance is driven only by **propagating** labels — those applied to a whole
    cluster. ``propagating`` is the set of tx_hashes whose label should propagate to
    unlabeled cluster siblings (cluster-sourced labels); ``None`` means *all* explicit
    labels propagate (legacy behaviour). A single-tx (manual) label thus colours its
    own tx via per-tx precedence but never its siblings. A cluster inherits
    ``malicious`` if any propagating member is malicious, else ``benign`` if any is
    benign (malicious wins on conflict). The noise bucket (``cluster_id == -1``) never
    propagates inheritance.

    Returns ``(tx_verdict, cluster_info)`` where ``tx_verdict[tx] ∈ {malicious,
    benign, anomaly, normal}`` and ``cluster_info[cid] = {verdict, conflict,
    labeled_count, anomaly_count}``. ``verdict`` is the inheritable (cluster-applied)
    verdict; ``conflict``/``labeled_count`` reflect *all* explicit member labels (so a
    manual override inside a labeled cluster still shows as a conflict); ``anomaly_count``
    = members with ``votes >= anomaly_threshold``, independent of any label.
    """
    prop = set(explicit) if propagating is None else propagating

    members: dict[int, list[str]] = {}
    for tx, cid in cluster_of.items():
        members.setdefault(cid, []).append(tx)

    cluster_info: dict[int, dict[str, Any]] = {}
    inherited: dict[int, str | None] = {}
    for cid, txs in members.items():
        labeled = [explicit[t] for t in txs if t in explicit]
        has_mal = VERDICT_MALICIOUS in labeled
        has_ben = VERDICT_BENIGN in labeled
        # Only cluster-applied (propagating) labels set the inheritable verdict.
        prop_labeled = [explicit[t] for t in txs if t in explicit and t in prop]
        verdict = None if cid == -1 else _dominant_verdict(prop_labeled)
        inherited[cid] = verdict
        cluster_info[cid] = {
            "verdict": verdict,
            "conflict": has_mal and has_ben,
            "labeled_count": len(labeled),
            "anomaly_count": sum(1 for t in txs if votes.get(t, 0) >= anomaly_threshold),
        }

    tx_verdict: dict[str, str] = {}
    for tx, cid in cluster_of.items():
        ex = explicit.get(tx)
        inh = inherited.get(cid)
        if ex in CLUSTER_VERDICTS:
            tx_verdict[tx] = ex
        elif inh is not None:
            tx_verdict[tx] = inh
        elif votes.get(tx, 0) >= anomaly_threshold:
            tx_verdict[tx] = VERDICT_ANOMALY
        else:
            tx_verdict[tx] = VERDICT_NORMAL
    return tx_verdict, cluster_info


def _anomaly_votes(
    repo: Repo, target: str, feature_set: str, *, near: str | None = None
) -> dict[str, int]:
    """Per-tx anomaly votes from the anomaly run paired with this cluster run, or
    ``{}`` if the target has no anomaly run for this feature set yet. ``near`` is the
    cluster run's ``created_at`` so we pick its sibling, not the newest run."""
    run_id = repo.latest_anomaly_run(target, feature_set, near=near)
    return repo.anomaly_votes_for_run(run_id) if run_id else {}


def _run_membership(
    repo: Repo, target: str, feature_set: str, *, near: str | None = None, canonical: bool = False
) -> tuple[dict[str, int], dict[str, Any] | None]:
    """Base tx→cluster membership for inheritance: the latest cluster run's labels,
    plus the run row itself (callers need its ``created_at`` to pair the anomaly run).
    ``near`` picks the run closest in time, its pipeline sibling, rather than the
    newest. Both are ``{}``/``None`` when the target has no cluster run for this feature
    set; inheritance then degrades to explicit labels only (still correct, since
    labelling a cluster writes explicit per-tx labels on every member).

    ``canonical=True`` resolves the System (canonical) run instead of the latest of any
    origin, and ignores ``near``: the host publish path uses it so a user's Custom run
    can never feed the host ``contract_anomaly`` feed. UI reads keep the default."""
    run = (
        repo.latest_canonical_run(target, feature_set)
        if canonical
        else repo.latest_cluster_run(target, feature_set, near=near)
    )
    cluster_of = dict(repo.run_tx_labels(run["run_id"])) if run else {}
    return cluster_of, run


def _resolve_with_labels(
    repo: Repo, target: str, cluster_of: dict[str, int], votes: dict[str, int]
) -> tuple[dict[str, str], dict[int, dict[str, Any]], dict[str, str]]:
    """``compute_verdicts`` paired with the target's explicit labels — the one
    source every read decorator uses, so they can't accidentally pass a different
    label set. ``compute_verdicts`` only reads labels for txs present in
    ``cluster_of``, so the target-wide label set can't leak across clusters. Only
    cluster-applied labels propagate (``cluster_labeled_hashes``); single-tx labels
    colour their own tx only. Also returns the own-label map so per-tx decorators can
    surface each tx's own explicit label without re-reading it."""
    labels = repo.labels_for_target(target)
    tx_verdict, cluster_info = compute_verdicts(
        cluster_of, labels, votes, propagating=repo.cluster_labeled_hashes(target)
    )
    return tx_verdict, cluster_info, labels


def _resolve_verdicts(
    repo: Repo, target: str, cluster_of: dict[str, int], votes: dict[str, int]
) -> tuple[dict[str, str], dict[int, dict[str, Any]]]:
    """``_resolve_with_labels`` without the own-label map (for callers that only need
    the effective verdicts / cluster info)."""
    tx_verdict, cluster_info, _ = _resolve_with_labels(repo, target, cluster_of, votes)
    return tx_verdict, cluster_info


@dataclass(slots=True)
class _RunContext:
    """A run resolved for a verdict-decorated *view* of its own membership (the graph
    and projection payloads). ``tx_verdict`` is resolved over the FULL membership so a
    displayed tx still inherits from a labeled sibling capped out of the shown subset."""

    run: dict[str, Any]
    target: str
    feature_set: str
    labels: dict[str, int]
    tx_verdict: dict[str, str]


def _resolve_run(repo: Repo, run_id: str) -> _RunContext:
    """Load a run by id and resolve its per-tx verdicts. Raises ``KeyError`` for an
    unknown run. Shared scaffolding for ``build_graph`` / ``build_projection``."""
    run = repo.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    target = run["target"]
    feature_set = run["feature_set"]
    labels = repo.run_tx_labels(run_id)
    votes = _anomaly_votes(repo, target, feature_set, near=run["created_at"])
    tx_verdict, _ = _resolve_verdicts(repo, target, labels, votes)
    return _RunContext(run, target, feature_set, labels, tx_verdict)


def _subset_membership(
    labels: dict[str, int],
    *,
    limit: int,
    cluster: int | None = None,
    keep: Container[str] | None = None,
) -> tuple[list[tuple[str, int]], int]:
    """Select the (tx, cluster) pairs to display: optionally restricted to ``keep``
    (txs we have data for) and a single ``cluster``, sorted clustered-first so a
    capped view stays informative, then truncated to ``limit``. Returns the capped
    subset and the pre-cap total."""
    items = [(tx, cid) for tx, cid in labels.items() if keep is None or tx in keep]
    if cluster is not None:
        items = [(tx, cid) for tx, cid in items if cid == cluster]
    items.sort(key=lambda kv: (kv[1] == -1, kv[1]))
    return items[:limit], len(items)


def _stamp_verdicts(
    rows: list[dict[str, Any]],
    tx_verdict: dict[str, str],
    labels: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Write each row's effective ``verdict`` (default ``normal``), keyed by ``tx_hash``.
    When ``labels`` is given, also write each row's own explicit ``label`` — the verdict
    applied to that tx itself, or ``None`` — so the UI can offer ``clear`` only when there
    is a label to remove (an inherited verdict has no own label). Mutates and returns
    ``rows`` — the shared tail of every per-tx read decorator."""
    for r in rows:
        h = r["tx_hash"]
        r["verdict"] = tx_verdict.get(h, VERDICT_NORMAL)
        if labels is not None:
            r["label"] = labels.get(h)
    return rows


def cluster_summary_with_verdicts(
    repo: Repo,
    run_id: str,
    target: str,
    feature_set: str,
    *,
    run_created_at: str | None = None,
) -> list[dict[str, Any]]:
    """``repo.cluster_summary`` decorated with each cluster's manual verdict,
    conflict flag, label count and auto-anomaly member count."""
    rows = repo.cluster_summary(run_id, target)
    cluster_of = repo.run_tx_labels(run_id)
    votes = _anomaly_votes(repo, target, feature_set, near=run_created_at)
    _, cluster_info = _resolve_verdicts(repo, target, cluster_of, votes)

    for row in rows:
        info = cluster_info.get(row["cluster_id"], {})
        row["verdict"] = info.get("verdict")
        row["verdict_conflict"] = bool(info.get("conflict", False))
        row["labeled_count"] = int(info.get("labeled_count", 0))
        row["anomaly_count"] = int(info.get("anomaly_count", 0))
    return rows


def cluster_transactions_with_verdicts(
    repo: Repo,
    run_id: str,
    target: str,
    feature_set: str,
    cluster_id: int,
    *,
    limit: int,
    offset: int,
    run_created_at: str | None = None,
) -> list[dict[str, Any]]:
    """``repo.cluster_transactions`` with an effective ``verdict`` and raw ``votes``
    on each row. Membership is scoped to THIS cluster (so we don't load the whole
    run's labels just to decorate one cluster's page); ``labels_for_target`` is
    cheap since only manually-labeled txs exist. Note: we pass no large hash list as
    a query parameter — ``cluster_member_hashes`` is a result set, not an ``IN``
    array — so a big cluster can't overflow the ClickHouse form-field limit."""
    rows = repo.cluster_transactions(run_id, target, cluster_id, limit=limit, offset=offset)
    members = repo.cluster_member_hashes(run_id, cluster_id)
    cluster_of = dict.fromkeys(members, cluster_id)
    votes = _anomaly_votes(repo, target, feature_set, near=run_created_at)
    tx_verdict, _, labels = _resolve_with_labels(repo, target, cluster_of, votes)
    _stamp_verdicts(rows, tx_verdict, labels)
    for row in rows:
        row["votes"] = int(votes.get(row["tx_hash"], 0))
    return rows


def _attach_anomaly_reasons(
    repo: Repo,
    target: str,
    feature_set: str,
    rows: list[dict[str, Any]],
    *,
    model_row: dict[str, Any] | None = None,
) -> None:
    """Attach a human-readable ``reasons`` list (top deviating shape features) to each
    auto-``anomaly`` row, in place. Computed only for flagged rows, so the cost is
    O(top-N) not O(all rows). Reasons are a best-effort decoration — any problem below
    simply leaves the rows without them, never failing the read.

    The baseline is the contract's *current* shape model (``center``/``scale``). That is
    exactly right for the live feed; for a historical run browsed in Outliers it's an
    approximation (the model may have been re-fit since), but the only persisted scaler
    is the latest one — and a model fit on a *different* run than the latest is the normal
    state (a canonical model vs a later custom re-cluster), so gating on an exact run match
    would suppress reasons almost everywhere. No-op when:

    * ``feature_set`` isn't ``shape`` (graph attribution would mean something else); or
    * nothing is flagged / the flagged txs have no shape features; or
    * no shape model is persisted; or
    * the model blob can't be deserialized (stale/pre-signing format or signing-key
      mismatch — ``deserialize_model`` raises by design; classification rebuilds it, but
      this read path must not 500 on it)."""
    if feature_set != "shape":
        return
    flagged = [r for r in rows if r.get("verdict") == VERDICT_ANOMALY]
    if not flagged:
        return
    if model_row is None:
        model_row = repo.latest_cluster_model(target, "shape")
    if model_row is None:
        return
    raw_df = repo.fetch_shape_features_for(target, [r["tx_hash"] for r in flagged])
    if raw_df.empty:
        return  # nothing to attribute — skip the (multi-MB) model load
    try:
        model = load_cluster_model(model_row)
    except ModelIntegrityError as exc:
        logger.warning("skipping anomaly reasons for %s: model blob unusable (%s)", target, exc)
        return
    reasons = explain_shape_deviations(raw_df, model.center, model.scale)
    for r in flagged:
        r["reasons"] = [asdict(d) for d in reasons.get(r["tx_hash"], [])]


def latest_interactions_with_verdicts(
    repo: Repo,
    target: str,
    feature_set: str = "shape",
    *,
    limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    """The latest ``limit`` transactions for a target (newest first), each decorated
    with a LIVE verdict — the recency-first feed behind the Latest tab.

    Unlike the cluster/anomaly views, this lists every recent tx regardless of whether
    it's been classified. Cluster membership (which drives inheritance) comes from the
    latest cluster run; new txs scored online carry their own cluster id from the frozen
    model. Those two only share a numbering when the model was fit on THAT run, so we
    fold the online ids in only then — otherwise a newer custom run's clusters would
    inherit labels across mismatched numbering (the bug ``online_classifications`` used
    to guard with a ``model_id`` filter). Resolution is the single precedence in
    ``compute_verdicts`` (explicit per-tx label > cluster label > anomaly > normal), so a
    cluster relabel is reflected here on the next load with no model rebuild.

    A tx that is in no run, isn't online-scored against the current run's model, and has
    no explicit label of its own gets ``classified = False`` and a ``None`` verdict so the
    UI shows ``unclassified`` rather than a misleading ``normal``. An explicit per-tx
    label always classifies the tx and wins, even with no run behind it.

    Returns ``{"target", "feature_set", "transactions": [...]}``."""
    rows = repo.latest_transactions(target, feature_set, limit=limit, offset=offset)

    cluster_of, cluster_run = _run_membership(repo, target, feature_set)
    votes = (
        _anomaly_votes(repo, target, feature_set, near=cluster_run["created_at"])
        if cluster_run
        else {}
    )
    # Online signals use the frozen model's cluster numbering; trust them only when that
    # model was fit on this same run (the common path right after process_contract) AND it
    # is current-schema. A pre-v3 model's stored `votes` use the old noise-inclusive
    # semantics, so re-deriving a verdict from them would render stale anomalies until a
    # classify rebuilds + re-scores the contract; until then, ignore its online signals.
    model = repo.latest_cluster_model(target, feature_set)
    trust_online = bool(
        cluster_run
        and model
        and model.get("schema_version") == MODEL_SCHEMA_VERSION
        and model["run_id"] == cluster_run["run_id"]
    )
    if trust_online:
        for r in rows:
            online_cid = r.get("online_cluster_id")
            if online_cid is not None:  # online_votes is non-NULL iff online_cid is
                cluster_of[r["tx_hash"]] = int(online_cid)
                # online_votes counts independent detectors only (0..2, noise
                # excluded — see clustering.model.score_shape). The auto-anomaly
                # threshold below (FLAG_VOTE_THRESHOLD == 2) matches score_shape's
                # "all detectors agree" rule because both detectors are always fit
                # together; keep the two in lockstep if that ever changes.
                votes[r["tx_hash"]] = int(r["online_votes"])

    tx_verdict, _, labels = _resolve_with_labels(repo, target, cluster_of, votes)
    for r in rows:
        h = r["tx_hash"]
        own = labels.get(h)
        verdict: str | None
        if h in cluster_of:
            verdict = tx_verdict.get(h, VERDICT_NORMAL)
        else:
            verdict = own  # explicit per-tx label wins even with no run; else None
        r["verdict"] = verdict
        r["classified"] = verdict is not None
        r["label"] = own
        r["cluster_id"] = cluster_of.get(h)
        r["votes"] = int(votes.get(h, 0))
    _attach_anomaly_reasons(repo, target, feature_set, rows, model_row=model)
    return {"target": target, "feature_set": feature_set, "transactions": rows}


def top_anomalies_with_verdicts(
    repo: Repo, run_id: str, *, limit: int, offset: int = 0
) -> dict[str, Any]:
    """The run's top anomaly candidates, each decorated with its effective verdict.

    The score columns (consensus/votes/iso/lof/dbscan) are the unsupervised *evidence*;
    ``verdict`` is the human-judgement axis that can override it, resolved with the single
    precedence in ``compute_verdicts`` (explicit per-tx label > cluster-inherited label >
    auto-anomaly at ``votes >= threshold`` > normal). Inheritance uses the cluster run
    closest in time to THIS anomaly run (its pipeline sibling, via ``near=``) — so a
    historical run resolves membership against its contemporaneous clustering, not a newer
    one; a ``graph`` anomaly run has no cluster run, so it degrades to explicit labels
    only - still correct, since labelling a cluster writes explicit per-tx labels on every
    member. Votes are this run's own (0-3 shape / 0-2 graph), so the auto part of the
    verdict reflects the run you're viewing. Raises ``KeyError`` if the run is unknown
    (the caller maps it to 404). Returns ``{"run_id", "run", "candidates"}``."""
    run = repo.get_anomaly_run(run_id)
    if run is None:
        raise KeyError(run_id)
    target = run["target"]
    rows = repo.top_anomalies(run_id, target, limit=limit, offset=offset)

    cluster_of, _ = _run_membership(repo, target, run["feature_set"], near=run["created_at"])
    votes: dict[str, int] = {}
    for r in rows:
        cluster_of.setdefault(r["tx_hash"], -1)  # not in the cluster run → noise, no inherit
        votes[r["tx_hash"]] = int(r["votes"])
    tx_verdict, _, labels = _resolve_with_labels(repo, target, cluster_of, votes)
    _stamp_verdicts(rows, tx_verdict, labels)
    # Attribute reasons only on the LATEST anomaly run: the baseline is the current model,
    # which describes today's population. Explaining a historical run (the UI's run picker
    # allows it) against it could show "why flagged" chips that weren't true under that
    # run's own scaler, so omit them there rather than mislead.
    if run_id == repo.latest_anomaly_run(target, run["feature_set"]):
        _attach_anomaly_reasons(repo, target, run["feature_set"], rows)
    return {"run_id": run_id, "run": run, "candidates": rows}
