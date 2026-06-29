"""Unit tests for the pure ``compute_verdicts`` precedence + cluster inheritance.

No DB / sklearn — exercises the single source of truth for how explicit labels,
cluster-membership inheritance and auto-anomaly votes resolve to an effective
per-tx verdict and a per-cluster summary.
"""

from __future__ import annotations

from app.service import compute_verdicts


def test_explicit_malicious_beats_votes_and_inherited_benign() -> None:
    # tx "a" is explicitly malicious; its cluster also has a benign-labeled "b".
    cluster_of = {"a": 0, "b": 0}
    explicit = {"a": "malicious", "b": "benign"}
    tx, info = compute_verdicts(cluster_of, explicit, votes={"a": 0})
    assert tx["a"] == "malicious"
    # Cluster conflict: malicious wins at cluster level, conflict flagged.
    assert info[0]["verdict"] == "malicious"
    assert info[0]["conflict"] is True
    assert info[0]["labeled_count"] == 2


def test_explicit_benign_suppresses_anomaly() -> None:
    cluster_of = {"a": 0}
    tx, _ = compute_verdicts(cluster_of, {"a": "benign"}, votes={"a": 3})
    assert tx["a"] == "benign"


def test_unlabeled_inherits_malicious_from_sibling() -> None:
    cluster_of = {"a": 1, "b": 1}
    tx, info = compute_verdicts(cluster_of, {"a": "malicious"}, votes={})
    assert tx["b"] == "malicious"  # inherited
    assert info[1]["verdict"] == "malicious"


def test_inherited_benign_suppresses_anomaly_for_unlabeled_sibling() -> None:
    cluster_of = {"a": 2, "b": 2}
    tx, _ = compute_verdicts(cluster_of, {"a": "benign"}, votes={"b": 3})
    assert tx["b"] == "benign"  # inherited benign beats votes


def test_explicit_benign_member_overrides_inherited_malicious() -> None:
    # Cluster inherits malicious (from "a"), but "b" is explicitly benign → benign.
    cluster_of = {"a": 0, "b": 0}
    explicit = {"a": "malicious", "b": "benign"}
    tx, _ = compute_verdicts(cluster_of, explicit, votes={})
    assert tx["b"] == "benign"


def test_noise_bucket_does_not_propagate() -> None:
    cluster_of = {"a": -1, "b": -1}
    tx, info = compute_verdicts(cluster_of, {"a": "malicious"}, votes={"b": 0})
    assert tx["a"] == "malicious"  # explicit still applies
    assert tx["b"] == "normal"  # no inheritance in noise
    assert info[-1]["verdict"] is None


def test_votes_without_labels_yield_anomaly_or_normal() -> None:
    cluster_of = {"hi": 0, "lo": 0}
    tx, _ = compute_verdicts(cluster_of, {}, votes={"hi": 2, "lo": 1})
    assert tx["hi"] == "anomaly"
    assert tx["lo"] == "normal"


def test_threshold_boundary_is_inclusive() -> None:
    cluster_of = {"a": 0, "b": 0}
    tx, _ = compute_verdicts(cluster_of, {}, votes={"a": 2, "b": 1}, anomaly_threshold=2)
    assert tx["a"] == "anomaly"
    assert tx["b"] == "normal"


def test_empty_inputs() -> None:
    tx, info = compute_verdicts({}, {}, {})
    assert tx == {}
    assert info == {}
