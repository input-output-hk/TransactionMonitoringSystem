"""Tests for the authoritative contract_anomaly projection (publish + retract).

The host reads ``tx_contract_anomaly`` directly, so a publish must not only ADD
current positives but RETRACT anything no longer flagged (re-fit reclassified it,
or a human labeled it benign / cleared a label). These pin that reconciliation
and the host-projection sync triggered on a label change. A small fake client
records the inserts/commands/queries the publish issues, so no ClickHouse needed.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.service.publish import (
    _COLUMNS,
    _VERSION_EPSILON,
    _publish_labels,
    _publish_online,
    _reconciliation_version,
    _retract_stale,
    publish_contract_anomaly,
)
from app.service.verdicts import VERDICT_MALICIOUS

_VERDICT_COL = _COLUMNS.index("verdict")
_TX_HASH_COL = _COLUMNS.index("tx_hash")
_PUBLISHED_AT_COL = _COLUMNS.index("published_at")
_PUB = datetime(2026, 6, 23, 12, 0, 0)  # a fixed reconciliation version for tests


class FakeClient:
    """Records inserts/commands and serves canned ``query`` result rows in order."""

    def __init__(self, query_rows: list[list[tuple[Any, ...]]] | None = None) -> None:
        self.inserts: list[tuple[str, list[list[Any]], list[str]]] = []
        self.commands: list[str] = []
        self.queries: list[str] = []
        self.query_params: list[dict[str, Any]] = []
        self._query_rows = list(query_rows or [])

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        self.queries.append(sql)
        self.query_params.append(parameters or {})
        rows = self._query_rows.pop(0) if self._query_rows else []
        return SimpleNamespace(result_rows=rows)

    def insert(
        self, table: str, data: list[list[Any]], column_names: list[str] | None = None
    ) -> None:
        self.inserts.append((table, data, column_names or []))

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> None:
        self.commands.append(sql)


def _repo(client: FakeClient) -> Any:
    return SimpleNamespace(client=client, _db="tms")


def test_retract_stale_tombstones_only_dropped_hashes() -> None:
    # Currently published (non-normal): txA, txC. Freshly flagged this run: txA, txB.
    # txC dropped out → it (and only it) must get a 'normal' tombstone.
    fake = FakeClient([[("txA",), ("txC",)]])
    n = _retract_stale(
        _repo(fake),
        "addr1",
        "preprod",
        "shape",
        keep={"txA", "txB"},
        published_at=_PUB,
    )
    assert n == 1
    table, rows, cols = fake.inserts[0]
    assert table == "tms.tx_contract_anomaly"
    assert len(rows) == 1
    assert rows[0][_TX_HASH_COL] == "txC"
    assert rows[0][_VERDICT_COL] == "normal"
    assert rows[0][_PUBLISHED_AT_COL] == _PUB  # carries the reconciliation version
    assert cols == _COLUMNS


def test_retract_stale_is_scoped_to_its_feature_set() -> None:
    # The currently-published scan must filter on feature_set: without it, a
    # reconciliation for one feature set would see (and tombstone) live rows
    # another feature set published for the same (network, target).
    fake = FakeClient([[("txA",)]])
    _retract_stale(
        _repo(fake),
        "addr1",
        "preprod",
        "graph",
        keep=set(),
        published_at=_PUB,
    )
    assert "feature_set = {fs:String}" in fake.queries[0]
    assert fake.query_params[0]["fs"] == "graph"


def test_retract_stale_noop_when_nothing_dropped() -> None:
    fake = FakeClient([[("txA",)]])
    n = _retract_stale(
        _repo(fake),
        "addr1",
        "preprod",
        "shape",
        keep={"txA", "txB"},
        published_at=_PUB,
    )
    assert n == 0
    assert fake.inserts == []


def test_publish_online_suppresses_benign_labeled_txs() -> None:
    # The SELECT/INSERT must exclude txs a human labeled benign (FINAL+deleted=0)
    # so a "cleared"/benign label retracts instead of re-publishing the anomaly.
    fake = FakeClient([[("txB",)]])  # the flagged-and-not-benign hash
    published = _publish_online(_repo(fake), "addr1", "preprod", "shape", _PUB)
    assert published == {"txB"}
    assert len(fake.commands) == 1  # the INSERT...SELECT
    sql = fake.commands[0]
    assert "tx_labels" in sql
    assert "label = 'benign'" in sql
    assert "deleted = 0" in sql
    assert "NOT IN" in sql


def test_publish_online_includes_malicious_labeled_txs() -> None:
    # A human malicious label must be published even if the model verdict was
    # normal (single-tx / noise / new-tx judgements), overridden to 'malicious'.
    fake = FakeClient([[("txB",)]])
    _publish_online(_repo(fake), "addr1", "preprod", "shape", _PUB)
    sql = fake.commands[0]
    assert "label = 'malicious'" in sql
    # The published verdict is overridden to malicious for labeled txs.
    assert "'malicious', toString(verdict)" in sql
    # And every row carries the reconciliation version.
    assert "AS published_at" in sql


def test_publish_online_noop_when_nothing_flagged() -> None:
    fake = FakeClient([[]])  # SELECT returns no flagged hashes
    published = _publish_online(_repo(fake), "addr1", "preprod", "shape", _PUB)
    assert published == set()
    assert fake.commands == []  # no INSERT issued


def test_publish_reconciles_then_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end orchestration with the two source paths stubbed: published =
    # {txA} ∪ {txB}; current non-normal = {txA, txC}; so txC is retracted and the
    # flagged-count query (verdict != normal) is returned. The canned table MAX
    # is ahead of now() (a backward clock step), so the pass must stamp its rows
    # just past the MAX rather than with the regressed wall clock.
    monkeypatch.setattr("app.service.publish._publish_online", lambda *a, **k: {"txB"})
    monkeypatch.setattr("app.service.publish._publish_batch", lambda *a, **k: {"txA"})
    # The manual-label path is exercised on its own below; stub it here so this
    # test stays focused on online+batch reconciliation.
    monkeypatch.setattr("app.service.publish._publish_labels", lambda *a, **k: set())
    stale_max = datetime(2200, 1, 1)  # far enough ahead to outlive any real now()
    fake = FakeClient(
        [
            [(stale_max,)],  # _reconciliation_version: table MAX(published_at)
            [("txA",), ("txC",)],  # _retract_stale: current non-normal hashes
            [(2,)],  # final flagged count
        ]
    )
    n = publish_contract_anomaly(_repo(fake), "addr1", network="preprod")
    assert n == 2
    # The pass derives its version before touching any rows.
    assert "max(published_at)" in fake.queries[0]
    # Exactly one tombstone insert, for the dropped txC only.
    assert len(fake.inserts) == 1
    _, rows, _ = fake.inserts[0]
    assert [r[_TX_HASH_COL] for r in rows] == ["txC"]
    assert rows[0][_VERDICT_COL] == "normal"
    # The tombstone's version supersedes the pre-step MAX despite the old clock.
    assert rows[0][_PUBLISHED_AT_COL] == stale_max + _VERSION_EPSILON
    # The final count query filters out tombstones.
    assert "verdict != {normal:String}" in fake.queries[-1]


def test_label_change_triggers_host_projection_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    # On the host_ch path, applying a label must reconcile the host projection so
    # the alert is retracted/raised immediately rather than on the next re-fit.
    calls: list[str] = []
    monkeypatch.setattr(
        "app.service.publish.publish_contract_anomaly",
        lambda repo, target, **k: calls.append(target),
    )
    monkeypatch.setattr(
        "app.service.labels.get_settings",
        lambda: Settings(CHAIN_SOURCE="host_ch", CARDANO_NETWORK="preprod"),
    )
    from app.service.labels import label_transaction

    repo = SimpleNamespace(set_tx_labels=lambda *a, **k: 1)
    label_transaction(repo, "addr1", "a" * 64, "benign")
    assert calls == ["addr1"]


def test_label_change_skips_sync_off_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "app.service.publish.publish_contract_anomaly",
        lambda repo, target, **k: calls.append(target),
    )
    monkeypatch.setattr(
        "app.service.labels.get_settings",
        lambda: Settings(CHAIN_SOURCE="other", CARDANO_NETWORK="preprod"),
    )
    from app.service.labels import label_transaction

    repo = SimpleNamespace(set_tx_labels=lambda *a, **k: 1)
    label_transaction(repo, "addr1", "a" * 64, "benign")
    assert calls == []  # non-host_ch source: no host projection to sync


def test_publish_labels_publishes_unscored_malicious() -> None:
    # A malicious manual label the online/batch paths can't reach (never-scored,
    # non-cluster) must be inserted as malicious and returned so the caller keeps
    # it (otherwise _retract_stale would tombstone it).
    fake = FakeClient([[("txMANUAL",), ("txKNOWN",)]])  # tx_labels malicious hashes
    labeled = _publish_labels(
        _repo(fake),
        "addr1",
        "preprod",
        "shape",
        _PUB,
        exclude={"txKNOWN"},
    )
    assert labeled == {"txMANUAL", "txKNOWN"}
    # Only the not-already-published hash is inserted, as a malicious row.
    assert len(fake.inserts) == 1
    _, rows, _ = fake.inserts[0]
    assert len(rows) == 1
    assert rows[0][_TX_HASH_COL] == "txMANUAL"
    assert rows[0][_VERDICT_COL] == VERDICT_MALICIOUS


def test_publish_labels_noop_when_all_already_published() -> None:
    fake = FakeClient([[("txKNOWN",)]])
    labeled = _publish_labels(
        _repo(fake),
        "addr1",
        "preprod",
        "shape",
        _PUB,
        exclude={"txKNOWN"},
    )
    assert labeled == {"txKNOWN"}
    assert fake.inserts == []  # nothing fresh to insert


def test_reconciliation_version_is_wall_clock_when_ahead_of_table_max() -> None:
    # Normal operation: the table MAX is in the past, so the version is a fresh
    # wall-clock stamp, not the stale MAX plus epsilon.
    past_max = datetime(2020, 1, 1)
    fake = FakeClient([[(past_max,)]])
    version = _reconciliation_version(_repo(fake), "addr1", "preprod")
    assert version > past_max
    assert version != past_max + _VERSION_EPSILON


def test_reconciliation_version_never_regresses_after_backward_clock_step() -> None:
    # The table MAX is AHEAD of now() (NTP correction, VM migration): stamping
    # the regressed wall clock would make every row this pass writes lose on
    # FINAL, so the version must step strictly past the MAX instead.
    future_max = datetime(2200, 1, 1)
    fake = FakeClient([[(future_max,)]])
    version = _reconciliation_version(_repo(fake), "addr1", "preprod")
    assert version == future_max + _VERSION_EPSILON


def test_reconciliation_version_handles_empty_table_and_string_timestamps() -> None:
    # Never-published target: ClickHouse max() over zero rows returns the epoch
    # zero value, which must resolve to now, never epoch + epsilon.
    epoch = datetime(1970, 1, 1)
    fake = FakeClient([[(epoch,)]])
    version = _reconciliation_version(_repo(fake), "addr1", "preprod")
    assert version > epoch
    assert version != epoch + _VERSION_EPSILON
    # Some driver paths surface DateTime64 as an ISO string; a string MAX ahead
    # of now() must still be parsed and stepped past, microseconds preserved.
    fake = FakeClient([[("2200-01-01 00:00:00.000005",)]])
    version = _reconciliation_version(_repo(fake), "addr1", "preprod")
    assert version == datetime(2200, 1, 1, 0, 0, 0, 5) + _VERSION_EPSILON


def test_reconciliation_version_tolerates_null_max() -> None:
    # A NULL MAX (driver returning None) must behave like a never-published
    # target rather than raising.
    fake = FakeClient([[(None,)]])
    version = _reconciliation_version(_repo(fake), "addr1", "preprod")
    assert version.year > 1970
