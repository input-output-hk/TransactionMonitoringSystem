"""Label writes (cluster + per-tx) and the propagation rules they obey.

Cluster labels propagate to unlabeled siblings and future members; single-tx
(manual) labels colour only their own transaction (see service/labels.py and
``compute_verdicts``' ``propagating`` set)."""

from __future__ import annotations

import pytest

from app.service import (
    clear_cluster_members,
    clear_transaction_label,
    compute_verdicts,
    label_cluster_members,
    label_transaction,
)
from tests.fakes import FakeGraphRepo, FakeRepoBase


def test_label_cluster_members_resolves_hashes_and_writes() -> None:
    repo = FakeGraphRepo(membership={"a": 0, "b": 0, "c": 1}, explicit={}, votes={})
    out = label_cluster_members(repo, "r1", "addr", 0, "malicious")
    assert out["labeled"] == 2
    assert repo.label_calls == [("addr", ["a", "b"], "malicious")]


def test_label_cluster_members_rejects_noise() -> None:
    repo = FakeGraphRepo(membership={"a": -1}, explicit={}, votes={})
    with pytest.raises(ValueError):
        label_cluster_members(repo, "r1", "addr", -1, "malicious")


def test_clear_cluster_members_tombstones_hashes() -> None:
    repo = FakeGraphRepo(membership={"a": 0, "b": 0}, explicit={}, votes={})
    out = clear_cluster_members(repo, "r1", "addr", 0)
    assert out["cleared"] == 2
    assert repo.clear_calls == [("addr", ["a", "b"])]


# --- Per-transaction labels: single tx, manual source, no propagation -------


class FakeTxLabelRepo(FakeRepoBase):
    """Records the tx_labels writes a per-tx label triggers."""

    def __init__(self) -> None:
        self.set_calls: list[tuple] = []
        self.clear_calls: list[tuple] = []

    def set_tx_labels(self, target, tx_hashes, label, *, source="cluster", note=""):
        self.set_calls.append((target, list(tx_hashes), label, source, note))
        return len(list(tx_hashes))

    def clear_tx_labels(self, target, tx_hashes):
        self.clear_calls.append((target, list(tx_hashes)))
        return len(list(tx_hashes))


def test_label_transaction_writes_single_manual_label() -> None:
    repo = FakeTxLabelRepo()
    out = label_transaction(repo, "addr", "tx1", "malicious", note="n")
    assert out == {"target": "addr", "tx_hash": "tx1", "verdict": "malicious", "labeled": 1}
    # one hash only, source 'manual_tx' (distinct from cluster labels) — does not propagate.
    assert repo.set_calls == [("addr", ["tx1"], "malicious", "manual_tx", "n")]


def test_label_transaction_rejects_non_verdict() -> None:
    for bad in ("anomaly", "normal", "bogus"):
        with pytest.raises(ValueError):
            label_transaction(FakeTxLabelRepo(), "addr", "tx1", bad)


def test_clear_transaction_label_tombstones_single() -> None:
    repo = FakeTxLabelRepo()
    out = clear_transaction_label(repo, "addr", "tx1")
    assert out == {"target": "addr", "tx_hash": "tx1", "cleared": 1}
    assert repo.clear_calls == [("addr", ["tx1"])]


# --- Propagation: only cluster-applied labels reach unlabeled siblings ------


def test_manual_label_does_not_propagate_to_siblings() -> None:
    # A,B,C in cluster 0; only A is labelled, and it's a single-tx (manual) label
    # (not in `propagating`). A is malicious; B,C must NOT inherit it.
    cluster_of = {"a": 0, "b": 0, "c": 0}
    tx_verdict, info = compute_verdicts(cluster_of, {"a": "malicious"}, {}, propagating=set())
    assert tx_verdict["a"] == "malicious"
    assert tx_verdict["b"] == "normal"
    assert tx_verdict["c"] == "normal"
    assert info[0]["verdict"] is None  # cluster itself is not malicious


def test_cluster_label_propagates_to_siblings() -> None:
    # Same shape, but A's label is cluster-applied (in `propagating`) → B,C inherit.
    cluster_of = {"a": 0, "b": 0, "c": 0}
    tx_verdict, info = compute_verdicts(cluster_of, {"a": "malicious"}, {}, propagating={"a"})
    assert tx_verdict["a"] == tx_verdict["b"] == tx_verdict["c"] == "malicious"
    assert info[0]["verdict"] == "malicious"


def test_manual_benign_overrides_malicious_cluster_without_clearing_it() -> None:
    # Cluster 0 is cluster-labelled malicious (A,C propagate); B is manually benign.
    # B reads benign, the cluster stays malicious, and the row flags a conflict.
    cluster_of = {"a": 0, "b": 0, "c": 0}
    explicit = {"a": "malicious", "b": "benign", "c": "malicious"}
    tx_verdict, info = compute_verdicts(cluster_of, explicit, {}, propagating={"a", "c"})
    assert tx_verdict["b"] == "benign"
    assert tx_verdict["a"] == tx_verdict["c"] == "malicious"
    assert info[0]["verdict"] == "malicious"
    assert info[0]["conflict"] is True


def test_propagating_none_keeps_legacy_all_propagate() -> None:
    # Back-compat: omitting `propagating` treats every explicit label as propagating.
    tx_verdict, _ = compute_verdicts({"a": 0, "b": 0}, {"a": "malicious"}, {})
    assert tx_verdict["b"] == "malicious"
