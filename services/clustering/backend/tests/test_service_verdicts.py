"""Verdict-decorated reads: the graph view, the latest-interactions feed (LIVE
verdicts) and the top-anomalies page (evidence + effective verdict)."""

from __future__ import annotations

import pandas as pd
import pytest

from app.clustering.model import MODEL_SCHEMA_VERSION
from app.service import (
    build_graph,
    build_projection,
    latest_interactions_with_verdicts,
    top_anomalies_with_verdicts,
)
from tests.fakes import FakeGraphRepo, FakeRepoBase

# --- build_graph verdict inheritance + cluster labeling --------------------



def test_build_graph_inherits_verdict_across_subset_boundary() -> None:
    # "b" is visible (limit=1); its labeled sibling "a" is capped out of the
    # subset but must still propagate malicious through full-membership inheritance.
    repo = FakeGraphRepo(membership={"b": 0, "a": 0}, explicit={"a": "malicious"}, votes={})
    g = build_graph(repo, "r1", limit=1)
    assert g["nodes"] == [{"id": "b", "cluster": 0, "verdict": "malicious"}]


def test_build_graph_marks_anomaly_when_unlabeled() -> None:
    repo = FakeGraphRepo(membership={"a": 0}, explicit={}, votes={"a": 2})
    g = build_graph(repo, "r1", limit=10)
    assert g["nodes"][0]["verdict"] == "anomaly"


# --- build_projection: feature-space PCA scatter, verdict-decorated ----------


def _shape_row(tx_hash: str, scale: float) -> dict[str, object]:
    """A shape-feature row; ``scale`` shifts the whole vector so two scales form
    two well-separated blobs PCA can spread apart."""
    return {
        "tx_hash": tx_hash, "fees": scale, "size": scale, "input_count": scale,
        "output_count": scale, "total_input_lovelace": scale * 10,
        "total_output_lovelace": scale * 10, "net_lovelace": scale,
        "distinct_assets": scale, "redeemer_count": scale,
        "hour_of_day": int(scale) % 24, "day_of_week": int(scale) % 7 + 1,
    }


class FakeProjectionRepo(FakeGraphRepo):
    """FakeGraphRepo plus the shape features build_projection rebuilds the matrix
    from. Two clusters at clearly different scales so the projection is non-trivial."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._shape = pd.DataFrame(
            [_shape_row(tx, 1.0 if cid == 0 else 100.0) for tx, cid in self._membership.items()]
        )

    def fetch_shape_features(self, target: str) -> pd.DataFrame:
        return self._shape


def test_build_projection_2d_nodes_carry_coords_and_verdicts() -> None:
    repo = FakeProjectionRepo(
        membership={"a": 0, "b": 0, "c": 1, "d": 1}, explicit={"a": "malicious"}, votes={}
    )
    out = build_projection(repo, "r1", dims=2, limit=10)
    assert out["dims"] == 2 and out["metric"] == "euclidean"
    assert out["total"] == 4 and out["shown"] == 4 and out["truncated"] is False
    by = {n["id"]: n for n in out["nodes"]}
    assert set(by) == {"a", "b", "c", "d"}
    assert all("x" in n and "y" in n and "z" not in n for n in out["nodes"])
    # 'a' is labeled malicious and shares cluster 0 with 'b', which inherits it.
    assert by["a"]["verdict"] == "malicious" and by["b"]["verdict"] == "malicious"
    assert by["c"]["verdict"] == "normal"
    # PCA axes are interpretable: one per displayed dim, each with a variance
    # fraction and named feature loadings drawn from the shape feature set.
    assert len(out["axes"]) == 2
    ax0 = out["axes"][0]
    assert 0.0 <= ax0["variance"] <= 1.0
    assert ax0["top_features"] and {"name", "weight"} <= ax0["top_features"][0].keys()
    assert all(isinstance(f["name"], str) and isinstance(f["weight"], float)
               for f in ax0["top_features"])


def test_build_projection_3d_adds_z() -> None:
    repo = FakeProjectionRepo(membership={"a": 0, "b": 0, "c": 1, "d": 1}, explicit={}, votes={})
    out = build_projection(repo, "r1", dims=3)
    assert out["dims"] == 3
    assert all("z" in n for n in out["nodes"])


def test_build_projection_caps_and_filters_by_cluster() -> None:
    repo = FakeProjectionRepo(membership={"a": 0, "b": 0, "c": 1, "d": 1}, explicit={}, votes={})
    capped = build_projection(repo, "r1", dims=2, limit=2)
    assert capped["shown"] == 2 and capped["total"] == 4 and capped["truncated"] is True

    only1 = build_projection(repo, "r1", dims=2, cluster=1)
    assert {n["id"] for n in only1["nodes"]} == {"c", "d"}




# --- Latest interactions: recency feed, unclassified txs surfaced ------------


def _verdict_of(rows, tx):
    return next(r["verdict"] for r in rows if r["tx_hash"] == tx)


def _ctx(tx_hash, *, online_cluster_id=None, online_votes=None):
    """A latest_transactions row: tx context + (nullable) online signals."""
    return {
        "tx_hash": tx_hash, "block_time": f"2026-01-01 10:00:0{tx_hash[-1]}",
        "fees": 1, "size": 1, "total_input_lovelace": 1, "total_output_lovelace": 1,
        "net_lovelace": 0, "input_count": 1, "output_count": 1, "distinct_assets": 0,
        "redeemer_count": 1, "online_cluster_id": online_cluster_id,
        "online_votes": online_votes,
    }


class FakeLatestRepo(FakeRepoBase):
    """Repo exposing what latest_interactions_with_verdicts touches. The feed (newest
    first) is: brand-new 'new3' (no online row, not in any run), online 'inc2'
    (cluster 0, 2 votes), batch member 'a1' (cluster 0, 0 anomaly votes). ``model_run``
    is the run the frozen model was fit on — when it differs from the latest cluster
    run ('r1') the online signals must NOT be trusted (mismatched cluster numbering)."""

    def __init__(self, *, explicit, has_cluster_run=True, model_run="r1",
                 model_schema=MODEL_SCHEMA_VERSION):
        self._explicit = explicit
        self._has_cluster_run = has_cluster_run
        self._model_run = model_run
        self._model_schema = model_schema

    def latest_transactions(self, target, feature_set, *, limit, offset=0):
        return [
            _ctx("new3"),
            _ctx("inc2", online_cluster_id=0, online_votes=2),
            _ctx("a1"),
        ]

    def latest_cluster_run(self, target, feature_set, *, near=None):
        return {"run_id": "r1", "created_at": "2026-01-01 09:00:00"} if self._has_cluster_run \
            else None

    def latest_cluster_model(self, target, feature_set):
        if not self._has_cluster_run:
            return None
        return {"model_id": "m1", "run_id": self._model_run, "schema_version": self._model_schema}

    def fetch_shape_features_for(self, target, tx_hashes):
        # Reasons attribution isn't exercised by these verdict tests — an empty frame
        # makes _attach_anomaly_reasons a no-op before it touches the (absent) model blob.
        return pd.DataFrame()

    def latest_anomaly_run(self, target, feature_set, *, near=None):
        return "ar1" if self._has_cluster_run else None

    def anomaly_votes_for_run(self, run_id):
        return {"a1": 0}  # batch member 'a1' has no anomaly votes

    def run_tx_labels(self, run_id):
        return {"a1": 0}  # batch membership: 'a1' in cluster 0

    def labels_for_target(self, target):
        return dict(self._explicit)

    def cluster_labeled_hashes(self, target):
        return set(self._explicit)


def test_latest_preserves_newest_first_order() -> None:
    out = latest_interactions_with_verdicts(FakeLatestRepo(explicit={}), "addr", limit=10)
    assert out["target"] == "addr" and out["feature_set"] == "shape"
    assert [r["tx_hash"] for r in out["transactions"]] == ["new3", "inc2", "a1"]


def test_latest_unclassified_tx_has_null_verdict() -> None:
    rows = latest_interactions_with_verdicts(FakeLatestRepo(explicit={}), "addr", limit=10)[
        "transactions"
    ]
    new = next(r for r in rows if r["tx_hash"] == "new3")
    assert new["classified"] is False and new["verdict"] is None and new["cluster_id"] is None


def test_latest_classified_txs_resolve_verdicts() -> None:
    rows = latest_interactions_with_verdicts(FakeLatestRepo(explicit={}), "addr", limit=10)[
        "transactions"
    ]
    # batch member with no votes/label → normal; online member with 2 votes → anomaly.
    assert _verdict_of(rows, "a1") == "normal"
    assert _verdict_of(rows, "inc2") == "anomaly"
    assert next(r for r in rows if r["tx_hash"] == "a1")["classified"] is True


def test_latest_ignores_online_signals_from_stale_schema_model() -> None:
    # A pre-vN model's stored votes use the old (noise-inclusive) semantics; until a
    # re-classify rebuilds it, its online signals must not drive verdicts — otherwise
    # 'inc2' would render as a stale anomaly.
    repo = FakeLatestRepo(explicit={}, model_schema=MODEL_SCHEMA_VERSION - 1)
    rows = latest_interactions_with_verdicts(repo, "addr", limit=10)["transactions"]
    inc2 = next(r for r in rows if r["tx_hash"] == "inc2")
    assert inc2["verdict"] is None and inc2["classified"] is False


def test_latest_verdict_inherits_current_cluster_label() -> None:
    # Label cluster 0 benign → both its batch ('a1') and online ('inc2') members go
    # benign, live, with no model rebuild (anomaly on 'inc2' suppressed).
    rows = latest_interactions_with_verdicts(
        FakeLatestRepo(explicit={"a1": "benign"}), "addr", limit=10
    )["transactions"]
    assert _verdict_of(rows, "a1") == "benign"
    assert _verdict_of(rows, "inc2") == "benign"


def test_latest_untrusted_without_model() -> None:
    # No cluster run / model: online cluster ids can't be aligned to any run, so they're
    # not trusted and every tx (including online-scored 'inc2') reads unclassified.
    rows = latest_interactions_with_verdicts(
        FakeLatestRepo(explicit={}, has_cluster_run=False), "addr", limit=10
    )["transactions"]
    assert all(r["classified"] is False and r["verdict"] is None for r in rows)


def test_latest_distrusts_online_when_model_fit_on_other_run() -> None:
    # A custom run ('r1') is newer than the model (fit on 'old'): the online cluster ids
    # carry the model's numbering, so folding them into this run's membership would
    # cross-contaminate inheritance. They must be ignored → 'inc2' reads unclassified.
    rows = latest_interactions_with_verdicts(
        FakeLatestRepo(explicit={}, model_run="old"), "addr", limit=10
    )["transactions"]
    inc = next(r for r in rows if r["tx_hash"] == "inc2")
    assert inc["classified"] is False and inc["verdict"] is None
    # the batch member from the latest run still resolves normally.
    assert _verdict_of(rows, "a1") == "normal"


def test_latest_explicit_label_classifies_unrun_tx() -> None:
    # 'new3' is in no run and not online-scored, but a manual label on it must win
    # (classified, verdict = the label) rather than show as unclassified.
    rows = latest_interactions_with_verdicts(
        FakeLatestRepo(explicit={"new3": "malicious"}), "addr", limit=10
    )["transactions"]
    new = next(r for r in rows if r["tx_hash"] == "new3")
    assert new["classified"] is True and new["verdict"] == "malicious"
    assert new["label"] == "malicious"


# --- Top anomalies: candidates decorated with the effective verdict ---------



class FakeAnomalyRepo(FakeRepoBase):
    """Repo exposing what top_anomalies_with_verdicts touches. The shape anomaly run
    scores 'a' (cluster 0, 3 votes), 'b' (cluster 0, 0 votes) and 'lone' (no cluster
    membership, 2 votes). The sibling cluster run puts a,b in cluster 0."""

    def __init__(self, *, explicit, feature_set="shape", has_cluster_run=True):
        self._explicit = explicit
        self._feature_set = feature_set
        self._has_cluster_run = has_cluster_run

    def get_anomaly_run(self, run_id):
        if run_id != "ar1":
            return None
        return {
            "run_id": "ar1", "target": "addr", "feature_set": self._feature_set,
            "created_at": "2026-01-01 10:00:00",
        }

    def top_anomalies(self, run_id, target, *, limit, offset=0):
        return [
            {"tx_hash": "a", "votes": 3},
            {"tx_hash": "b", "votes": 0},
            {"tx_hash": "lone", "votes": 2},
        ]

    def latest_cluster_model(self, target, feature_set):
        return None  # no frozen model in these tests → reasons attribution is skipped

    def latest_anomaly_run(self, target, feature_set, *, near=None):
        return "ar1"  # the run under view is the latest → reasons gate passes

    def latest_cluster_run(self, target, feature_set, *, near=None):
        self.near_seen = near
        return {"run_id": "cr1"} if self._has_cluster_run else None

    def run_tx_labels(self, run_id):
        return {"a": 0, "b": 0}

    def labels_for_target(self, target):
        return dict(self._explicit)

    def cluster_labeled_hashes(self, target):
        return set(self._explicit)


def test_top_anomalies_unknown_run_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        top_anomalies_with_verdicts(FakeAnomalyRepo(explicit={}), "missing", limit=10)


def test_top_anomalies_auto_anomaly_and_normal_without_labels() -> None:
    out = top_anomalies_with_verdicts(FakeAnomalyRepo(explicit={}), "ar1", limit=10)
    assert out["run_id"] == "ar1" and out["run"]["feature_set"] == "shape"
    # 'a' (3 votes) and 'lone' (2 votes) auto-flag; 'b' (0 votes, unlabeled) is normal.
    assert _verdict_of(out["candidates"], "a") == "anomaly"
    assert _verdict_of(out["candidates"], "lone") == "anomaly"
    assert _verdict_of(out["candidates"], "b") == "normal"


def test_top_anomalies_explicit_label_overrides_high_votes() -> None:
    # 'a' has 3 votes but is labelled benign → benign wins (anomaly suppressed).
    out = top_anomalies_with_verdicts(
        FakeAnomalyRepo(explicit={"a": "benign"}), "ar1", limit=10
    )
    assert _verdict_of(out["candidates"], "a") == "benign"


def test_top_anomalies_cluster_label_inherits_to_member() -> None:
    # 'a' labelled malicious → its clustermate 'b' (cluster 0, no own label, 0 votes)
    # inherits malicious from the cluster.
    out = top_anomalies_with_verdicts(
        FakeAnomalyRepo(explicit={"a": "malicious"}), "ar1", limit=10
    )
    assert _verdict_of(out["candidates"], "b") == "malicious"


def test_top_anomalies_pairs_sibling_cluster_run_by_time() -> None:
    # Inheritance must come from the cluster run contemporaneous with THIS anomaly
    # run (near=created_at), not the newest one — otherwise viewing a historical
    # run resolves membership against a later clustering.
    repo = FakeAnomalyRepo(explicit={})
    top_anomalies_with_verdicts(repo, "ar1", limit=10)
    assert repo.near_seen == "2026-01-01 10:00:00"


def test_top_anomalies_graph_run_without_cluster_run_falls_back() -> None:
    # A graph anomaly run has no sibling cluster run: no crash, explicit labels still
    # apply, inheritance is simply absent (no membership to inherit from).
    out = top_anomalies_with_verdicts(
        FakeAnomalyRepo(explicit={"a": "malicious"}, feature_set="graph", has_cluster_run=False),
        "ar1",
        limit=10,
    )
    assert _verdict_of(out["candidates"], "a") == "malicious"  # explicit label survives
    assert _verdict_of(out["candidates"], "lone") == "anomaly"  # votes only
    assert _verdict_of(out["candidates"], "b") == "normal"  # no label, no votes, no inherit


def test_top_anomalies_stamps_own_label_distinct_from_inherited() -> None:
    # 'a' is cluster-labelled benign; 'b' inherits benign from the cluster but has no
    # own label. The own-label field must distinguish them so the UI offers `clear`
    # only on 'a' (which has a label to remove), not on 'b' (inherited only).
    out = top_anomalies_with_verdicts(FakeAnomalyRepo(explicit={"a": "benign"}), "ar1", limit=10)
    by = {c["tx_hash"]: c for c in out["candidates"]}
    assert by["a"]["verdict"] == "benign" and by["a"]["label"] == "benign"
    assert by["b"]["verdict"] == "benign" and by["b"]["label"] is None  # inherited, not own
