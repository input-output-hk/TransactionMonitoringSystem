"""Tests for the pre-deployment history backfill (service/history.py).

The boundary math, the skip-fast/marker discipline, the slot-capped insert
proxy, and both flavors' control flow are pinned against fakes: a canned
ClickHouse client for the boundary queries, a recording repo for cursor/insert
traffic, an httpx.MockTransport for the kupo host-API calls. No network, no
ClickHouse.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest

from app.config import Settings
from app.ingest.ingester import IngestResult
from app.models import AssetRecord, TxRecord, UtxoRecord
from app.service.history import (
    ROLLBACK_SAFETY_BLOCKS,
    ROLLBACK_SAFETY_SLOTS,
    BlockfrostHistory,
    HostBoundary,
    KupoHistory,
    _SlotCappedRepo,
    get_history_backfill,
    history_incomplete,
    host_history_boundary,
)
from tests.fakes import FakeRepoBase
from tests.test_hybrid_repo import _tx_record

_BF_SETTINGS = Settings(
    CHAIN_SOURCE="host_ch", HISTORY_SOURCE="blockfrost", BLOCKFROST_PROJECT_ID="k"
)
_KUPO_SETTINGS = Settings(
    CHAIN_SOURCE="host_ch",
    HISTORY_SOURCE="kupo",
    HOST_API_URL="http://app:8000",
    HOST_API_KEY="secret",
)


# --- factory -----------------------------------------------------------------------


def test_factory_none_when_disabled() -> None:
    assert get_history_backfill(Settings(CHAIN_SOURCE="host_ch")) is None
    # history_source without host_ch is rejected at startup; the factory's own
    # history_enabled check keeps it inert even if constructed directly.
    assert (
        get_history_backfill(Settings(CHAIN_SOURCE="blockfrost", HISTORY_SOURCE="blockfrost"))
        is None
    )


def test_factory_selects_flavor() -> None:
    assert isinstance(get_history_backfill(_BF_SETTINGS), BlockfrostHistory)
    assert isinstance(get_history_backfill(_KUPO_SETTINGS), KupoHistory)


# --- boundary math -----------------------------------------------------------------


class _BoundaryClient:
    """Serves the boundary's three aggregates in call order: (tip), (target
    floor slot + count), (target floor height)."""

    def __init__(self, rows_per_call: list[list[tuple[Any, ...]]]) -> None:
        self._rows = list(rows_per_call)
        self.sqls: list[str] = []

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        from types import SimpleNamespace

        self.sqls.append(sql)
        return SimpleNamespace(result_rows=self._rows.pop(0))

    def close(self) -> None:
        pass


def _boundary(monkeypatch: pytest.MonkeyPatch, rows: list[list[tuple[Any, ...]]]) -> Any:
    client = _BoundaryClient(rows)
    monkeypatch.setattr("app.service.history.connect", lambda settings: client)
    return host_history_boundary(_BF_SETTINGS, "addr1demo"), client


_TIP_SLOT = 100_000_000
_TIP_HEIGHT = 12_000_000


def test_boundary_least_of_floor_and_tip_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    # Target floor well below tip-safety → the target floor wins both terms.
    b, client = _boundary(
        monkeypatch,
        [
            [(_TIP_SLOT, _TIP_HEIGHT)],
            [(50_000_000, 4_321)],
            [(6_000_000,)],
        ],
    )
    assert b == HostBoundary(floor_slot=50_000_000, floor_height=6_000_000, host_tx_count=4_321)
    # Zero-slot MV rows must not poison the floor: the aggregate is conditional.
    assert "minIf(slot, slot > 0)" in client.sqls[1]


def test_boundary_tip_safety_caps_a_fresh_target(monkeypatch: pytest.MonkeyPatch) -> None:
    # A target first seen minutes ago: its floor sits inside rollback range, so
    # the tip-minus-safety term must win or backfilled rows could be fork ghosts.
    b, _ = _boundary(
        monkeypatch,
        [
            [(_TIP_SLOT, _TIP_HEIGHT)],
            [(_TIP_SLOT - 10, 3)],
            [(_TIP_HEIGHT - 2,)],
        ],
    )
    assert b is not None
    assert b.floor_slot == _TIP_SLOT - ROLLBACK_SAFETY_SLOTS
    assert b.floor_height == _TIP_HEIGHT - ROLLBACK_SAFETY_BLOCKS


def test_boundary_host_unknown_target_uses_tip_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    b, _ = _boundary(
        monkeypatch,
        [
            [(_TIP_SLOT, _TIP_HEIGHT)],
            [(0, 0)],
            [(0,)],
        ],
    )
    assert b is not None
    assert b.floor_slot == _TIP_SLOT - ROLLBACK_SAFETY_SLOTS
    assert b.host_tx_count == 0


def test_boundary_defers_when_no_tip(monkeypatch: pytest.MonkeyPatch) -> None:
    b, _ = _boundary(monkeypatch, [[(0, 0)]])
    assert b is None


def test_boundary_defers_when_younger_than_safety_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing is provably immutable on a chain shorter than the safety window.
    b, _ = _boundary(monkeypatch, [[(ROLLBACK_SAFETY_SLOTS - 1, ROLLBACK_SAFETY_BLOCKS - 1)]])
    assert b is None


# --- the slot-capped insert proxy ---------------------------------------------------


class _RecordingRepo(FakeRepoBase):
    def __init__(self, cursor: dict[str, Any] | None = None, requested_max_txs: int = 0) -> None:
        self.txs: list[Any] = []
        self.utxos: list[Any] = []
        self.assets: list[Any] = []
        self.cursors: list[dict[str, Any]] = []
        self._cursor = cursor
        # The per-contract "latest N to cluster on" the window-full skip gate
        # reads back via get_contract. 0 (default) resolves to the window
        # ceiling in effective_window_txs, i.e. the pre-per-contract skip
        # threshold — so tests that do not exercise a specific N keep their
        # original behavior unchanged.
        self._requested_max_txs = requested_max_txs

    def get_contract(self, target: str) -> dict[str, Any] | None:
        return {"requested_max_txs": self._requested_max_txs}

    def insert_transactions(self, rows: list[Any]) -> None:
        self.txs.extend(rows)

    def insert_utxos(self, rows: list[Any]) -> None:
        self.utxos.extend(rows)

    def insert_assets(self, rows: list[Any]) -> None:
        self.assets.extend(rows)

    def get_cursor(self, target: str) -> dict[str, Any] | None:
        return self._cursor

    def upsert_cursor(self, target: str, target_type: str, **kw: Any) -> None:
        self.cursors.append(kw)

    def close(self) -> None:
        pass


def _tx_at_slot(tx_hash: str, slot: int) -> TxRecord:
    tx = _tx_record()
    tx.tx_hash = tx_hash
    tx.slot = slot
    return tx


def test_slot_capped_repo_drops_rows_at_or_above_boundary_and_their_utxos() -> None:
    repo = _RecordingRepo()
    capped = _SlotCappedRepo(repo, 1_000, lambda _m: None)
    keep, drop = _tx_at_slot("aa" * 32, 999), _tx_at_slot("bb" * 32, 1_000)
    capped.insert_transactions([keep, drop])
    capped.insert_utxos(
        [
            UtxoRecord(
                target="t", tx_hash=keep.tx_hash, role="input", idx=0, address="a", lovelace=1
            ),
            UtxoRecord(
                target="t", tx_hash=drop.tx_hash, role="input", idx=0, address="a", lovelace=1
            ),
        ]
    )
    capped.insert_assets(
        [AssetRecord(target="t", tx_hash=drop.tx_hash, role="output", idx=0, unit="u", quantity=1)]
    )
    assert [t.tx_hash for t in repo.txs] == [keep.tx_hash]
    assert [u.tx_hash for u in repo.utxos] == [keep.tx_hash]
    assert repo.assets == []


# --- BlockfrostHistory --------------------------------------------------------------


def _patch_repo(monkeypatch: pytest.MonkeyPatch, repo: _RecordingRepo) -> None:
    monkeypatch.setattr("app.service.history.ClickHouseRepo", lambda settings: repo)


def _patch_boundary(monkeypatch: pytest.MonkeyPatch, boundary: HostBoundary | None) -> None:
    monkeypatch.setattr(
        "app.service.history.host_history_boundary", lambda settings, target: boundary
    )


_BOUNDARY = HostBoundary(floor_slot=50_000_000, floor_height=6_000_000, host_tx_count=100)


async def test_skip_fast_on_done_cursor_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _RecordingRepo(
        cursor={"source": "blockfrost_history", "done": 1, "txs_seen": 500, "cursor": "page:5"}
    )
    _patch_repo(monkeypatch, repo)
    # Boundary must NOT be consulted on the fast path (it costs three queries).
    _patch_boundary(monkeypatch, None)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "skipped" and out.txs_ingested == 500


async def test_skip_fast_on_max_reached_cursor_at_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ingester's max_reached terminal state — done=0, txs_seen == cap, page
    # cursor kept for a future raised-cap resume — is the COMMON outcome for an
    # address with more pre-boundary history than its cap. It must skip-fast
    # exactly like a done marker: without this, every classify tick recomputes
    # the boundary and re-enters ingest forever.
    repo = _RecordingRepo(
        cursor={
            "source": "blockfrost_history",
            "done": 0,
            "txs_seen": 500,
            "cursor": "page:5;from:100",
        }
    )
    _patch_repo(monkeypatch, repo)
    _patch_boundary(monkeypatch, None)  # must not be consulted

    async def _boom(**kw: Any) -> IngestResult:
        raise AssertionError("ingest must not run on an at-cap cursor")

    monkeypatch.setattr("app.service.history.ingest", _boom)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "skipped" and out.txs_ingested == 500


async def test_rewalk_when_cap_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    # txs_seen below the (raised) cap falls through the skip guard and re-walks.
    repo = _RecordingRepo(
        cursor={"source": "blockfrost_history", "done": 1, "txs_seen": 500, "cursor": "page:5"}
    )
    _patch_repo(monkeypatch, repo)
    _patch_boundary(monkeypatch, _BOUNDARY)
    seen: dict[str, Any] = {}

    async def _ingest(**kw: Any) -> IngestResult:
        seen.update(kw)
        return IngestResult("addr1demo", "address", "completed", 900, "page:9")

    monkeypatch.setattr("app.service.history.ingest", _ingest)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=1_000, progress=lambda _m: None
    )
    assert out.status == "completed" and out.txs_ingested == 900
    assert seen["max_txs"] == 1_000


async def test_passes_to_block_and_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    _patch_boundary(monkeypatch, _BOUNDARY)
    seen: dict[str, Any] = {}

    async def _ingest(**kw: Any) -> IngestResult:
        seen.update(kw)
        return IngestResult("addr1demo", "address", "completed", 10, "page:1")

    monkeypatch.setattr("app.service.history.ingest", _ingest)
    await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert seen["recent"] is True
    # to_block is inclusive; the boundary block holds the target's earliest
    # HOST row, so the walk stops one below it.
    assert seen["to_block"] == str(_BOUNDARY.floor_height - 1)
    assert seen["address"] == "addr1demo"


async def test_effective_cap_clamped_by_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    _patch_boundary(monkeypatch, _BOUNDARY)
    seen: dict[str, Any] = {}

    async def _ingest(**kw: Any) -> IngestResult:
        seen.update(kw)
        return IngestResult("addr1demo", "address", "completed", 0, "")

    monkeypatch.setattr("app.service.history.ingest", _ingest)
    await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=999_999, progress=lambda _m: None
    )
    assert seen["max_txs"] == _BF_SETTINGS.history_max_txs_ceiling


async def test_window_full_preflight_skips_and_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    full = HostBoundary(
        floor_slot=50_000_000,
        floor_height=6_000_000,
        host_tx_count=_BF_SETTINGS.clustering_window_txs,
    )
    _patch_boundary(monkeypatch, full)

    async def _boom(**kw: Any) -> IngestResult:
        raise AssertionError("no quota may be spent when the window is already full")

    monkeypatch.setattr("app.service.history.ingest", _boom)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "skipped" and "window full" in out.note
    # Marked done at the cap so later ticks skip-fast (the window never frees up).
    assert repo.cursors[-1]["done"] is True
    assert repo.cursors[-1]["source"] == "blockfrost_history"
    assert repo.cursors[-1]["txs_seen"] == 500


async def test_window_full_gate_fires_at_the_per_contract_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The window-full skip now keys on the contract's OWN "latest N to cluster
    # on", not the global ceiling: a contract with N=1000 whose host rows already
    # reach 1000 skips the top-up (older history would sit past its LIMIT), even
    # though the host count is far below the 50k ceiling the old gate used.
    repo = _RecordingRepo(requested_max_txs=1_000)
    _patch_repo(monkeypatch, repo)
    at_n = HostBoundary(floor_slot=50_000_000, floor_height=6_000_000, host_tx_count=1_000)
    _patch_boundary(monkeypatch, at_n)

    async def _boom(**kw: Any) -> IngestResult:
        raise AssertionError("no quota may be spent once the host fills the contract's window")

    monkeypatch.setattr("app.service.history.ingest", _boom)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=1_000, progress=lambda _m: None
    )
    assert out.status == "skipped" and "window full" in out.note
    assert repo.cursors[-1]["done"] is True


async def test_thin_contract_below_its_n_still_backfills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Recall-preservation counterpart: a contract with N=1000 whose host rows are
    # BELOW 1000 must STILL backfill its pre-deployment history — the window-full
    # gate must not starve a contract that has not yet reached its own N. (The old
    # gate, keyed on the 50k ceiling, would also have backfilled here; this pins
    # that the per-contract gate keeps doing so.)
    repo = _RecordingRepo(requested_max_txs=1_000)
    _patch_repo(monkeypatch, repo)
    thin = HostBoundary(floor_slot=50_000_000, floor_height=6_000_000, host_tx_count=500)
    _patch_boundary(monkeypatch, thin)
    seen: dict[str, Any] = {}

    async def _ingest(**kw: Any) -> IngestResult:
        seen.update(kw)
        return IngestResult("addr1demo", "address", "completed", 500, "page:5")

    monkeypatch.setattr("app.service.history.ingest", _ingest)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=1_000, progress=lambda _m: None
    )
    assert out.status == "completed" and seen["max_txs"] == 1_000


def _min_host_settings(min_host: int) -> Settings:
    return Settings(
        CHAIN_SOURCE="host_ch",
        HISTORY_SOURCE="blockfrost",
        BLOCKFROST_PROJECT_ID="k",
        HISTORY_MIN_HOST_TXS=min_host,
    )


async def test_shortfall_gate_skips_at_or_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A target the host already holds >= HISTORY_MIN_HOST_TXS rows for skips the
    # pre-deployment top-up and marks done (skip-fast), no provider quota spent.
    # host_tx_count == the threshold exercises the inclusive >= boundary.
    settings = _min_host_settings(1_000)
    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    at_threshold = HostBoundary(
        floor_slot=50_000_000,
        floor_height=6_000_000,
        host_tx_count=settings.history_min_host_txs,
    )
    _patch_boundary(monkeypatch, at_threshold)

    async def _boom(**kw: Any) -> IngestResult:
        raise AssertionError("no quota may be spent when the host sample is sufficient")

    monkeypatch.setattr("app.service.history.ingest", _boom)
    out = await BlockfrostHistory(settings).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "skipped" and "backfill not needed" in out.note
    assert repo.cursors[-1]["done"] is True
    assert repo.cursors[-1]["txs_seen"] == 500


async def test_shortfall_gate_on_still_backfills_thin_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Recall-preservation: with the gate ON, a genuinely thin contract (host rows
    # just BELOW the threshold) must STILL backfill its missing pre-deployment
    # history. The gate spares well-populated contracts; it must never starve a
    # sparse one — the case this change has to preserve.
    settings = _min_host_settings(1_000)
    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    thin = HostBoundary(
        floor_slot=50_000_000,
        floor_height=6_000_000,
        host_tx_count=settings.history_min_host_txs - 1,
    )
    _patch_boundary(monkeypatch, thin)
    seen: dict[str, Any] = {}

    async def _ingest(**kw: Any) -> IngestResult:
        seen.update(kw)
        return IngestResult("addr1demo", "address", "completed", 42, "page:1")

    monkeypatch.setattr("app.service.history.ingest", _ingest)
    out = await BlockfrostHistory(settings).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "completed" and seen["max_txs"] == 500


async def test_shortfall_gate_off_by_default_still_backfills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default HISTORY_MIN_HOST_TXS=0 preserves always-top-up: a well-populated
    # target below the window still backfills its missing pre-deployment history.
    assert _BF_SETTINGS.history_min_host_txs == 0  # guards the "off" premise
    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    populated = HostBoundary(floor_slot=50_000_000, floor_height=6_000_000, host_tx_count=2_746)
    _patch_boundary(monkeypatch, populated)
    seen: dict[str, Any] = {}

    async def _ingest(**kw: Any) -> IngestResult:
        seen.update(kw)
        return IngestResult("addr1demo", "address", "completed", 500, "page:5")

    monkeypatch.setattr("app.service.history.ingest", _ingest)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "completed" and seen["max_txs"] == 500


async def test_boundary_deferral_returns_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    _patch_boundary(monkeypatch, None)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "deferred"
    assert repo.cursors == []  # no marker: the next tick must retry


async def test_rate_limited_returns_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    _patch_boundary(monkeypatch, _BOUNDARY)

    async def _ingest(**kw: Any) -> IngestResult:
        return IngestResult("addr1demo", "address", "rate_limited", 120, "page:3")

    monkeypatch.setattr("app.service.history.ingest", _ingest)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "rate_limited" and out.txs_ingested == 120


async def test_no_pre_deployment_history_marks_done_not_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A target with genuinely no history below the boundary (SourceNotFound,
    # raised by discovery before any page) is a COMPLETED outcome, not an
    # error: it must mark done so later ticks skip-fast instead of re-asking
    # upstream on every classify tick forever.
    from app.sources.base import SourceNotFound

    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    _patch_boundary(monkeypatch, _BOUNDARY)

    async def _ingest(**kw: Any) -> IngestResult:
        raise SourceNotFound("no history for this address")

    monkeypatch.setattr("app.service.history.ingest", _ingest)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "completed" and out.txs_ingested == 0
    assert "no pre-deployment history" in out.note
    assert repo.cursors[-1]["done"] is True
    assert repo.cursors[-1]["source"] == "blockfrost_history"
    assert repo.cursors[-1]["txs_seen"] == 500  # settled at the cap, not 0


async def test_policy_target_deferred() -> None:
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="pol1", target_type="policy", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "deferred" and "address-only" in out.note


class _FakeHistorySource:
    """A minimal ChainSource standing in for BlockfrostSource, used ONLY by the
    wiring test below: every OTHER BlockfrostHistory test stubs ``ingest()``
    itself (see ``_patch_boundary``/``monkeypatch.setattr(...ingest...)``
    throughout this file), which cannot catch a regression that silently
    dropped the ``_SlotCappedRepo`` wrapper (``repo=capped`` reverted to
    ``repo=repo`` in ``_run``). This fake lets the REAL ``ingest()`` run
    end-to-end so the insert-time slot filter is exercised for real. Discovery
    yields every configured hash in one page (deliberately ignoring
    ``to_block``, a height bound); ``fetch_tx`` returns each at its own
    independently-set slot, decoupled from discovery, so the test isolates the
    _SlotCappedRepo's slot check specifically."""

    name = "blockfrost"
    host_backed = False
    # tx_hash -> slot, set per-test before construction.
    txs: ClassVar[dict[str, int]] = {}

    def __init__(self, _settings: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeHistorySource:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def tx_hash_pages(self, **_kw: Any) -> Any:
        yield "page:1", list(_FakeHistorySource.txs.keys())

    async def fetch_tx(self, target: str, target_type: str, tx_hash: str) -> Any:
        from app.sources.base import NormalizedTx

        tx = _tx_at_slot(tx_hash, _FakeHistorySource.txs[tx_hash])
        tx.target, tx.target_type = target, target_type
        return NormalizedTx(
            tx,
            [
                UtxoRecord(
                    target=target, tx_hash=tx_hash, role="input", idx=0, address="a", lovelace=1
                )
            ],
            [],
        )


async def test_slot_capped_repo_is_wired_into_the_real_ingest_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deliberately does NOT stub ingest(): proves _SlotCappedRepo is the repo
    # object the REAL ingest() writes through, so a regression reverting
    # `repo=capped` to `repo=repo` in _run would fail this test even though it
    # stubs nothing else about the insert path.
    below, at_boundary = "aa" * 32, "bb" * 32
    _FakeHistorySource.txs = {
        below: _BOUNDARY.floor_slot - 1,
        at_boundary: _BOUNDARY.floor_slot,
    }
    monkeypatch.setattr("app.blockfrost.source.BlockfrostSource", _FakeHistorySource)
    repo = _RecordingRepo()
    _patch_repo(monkeypatch, repo)
    _patch_boundary(monkeypatch, _BOUNDARY)

    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )

    assert out.status == "completed"
    assert {t.tx_hash for t in repo.txs} == {below}
    assert {u.tx_hash for u in repo.utxos} == {below}


# --- KupoHistory --------------------------------------------------------------------


def _kupo(
    monkeypatch: pytest.MonkeyPatch,
    repo: _RecordingRepo,
    handler: Any,
) -> tuple[KupoHistory, list[httpx.Request]]:
    _patch_repo(monkeypatch, repo)
    _patch_boundary(monkeypatch, _BOUNDARY)
    requests: list[httpx.Request] = []

    def _recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    flavor = KupoHistory(_KUPO_SETTINGS)
    monkeypatch.setattr(
        flavor,
        "_host_client",
        lambda: httpx.AsyncClient(
            base_url="http://app:8000", transport=httpx.MockTransport(_recording_handler)
        ),
    )
    return flavor, requests


async def test_kupo_trigger_returns_pending_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _RecordingRepo()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        import json

        body = json.loads(request.content)
        assert body["address"] == "addr1demo"
        assert body["created_before_slot"] == _BOUNDARY.floor_slot
        return httpx.Response(202, json={"status": "running"})

    flavor, requests = _kupo(monkeypatch, repo, handler)
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "pending"
    assert len(requests) == 1  # trigger-and-continue: no poll loop
    # A 202 records one attempt against the marker.
    assert repo.cursors[-1]["source"] == "kupo" and repo.cursors[-1]["done"] is False
    assert repo.cursors[-1]["cursor"] == "attempts:1"


async def test_kupo_409_foreign_job_writes_no_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # 409 = a FOREIGN backfill (an operator's manual latest-N run) holds the
    # same-address slot. It must NOT be adopted as ours (no marker written);
    # the next tick just retries.
    repo = _RecordingRepo()
    flavor, _ = _kupo(monkeypatch, repo, lambda r: httpx.Response(409, json={"detail": "busy"}))
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "pending"
    assert repo.cursors == []


async def test_kupo_503_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _RecordingRepo()
    flavor, _ = _kupo(monkeypatch, repo, lambda r: httpx.Response(503, json={"detail": "no kupo"}))
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "deferred"
    # A failure to even start a job now DOES spend an attempt (no_job-marked):
    # without this, a persistent 503 (no KUPO_URL configured) would retry
    # forever with no give-up signal (see test_kupo_gives_up_after_no_job_failures).
    assert repo.cursors[-1]["done"] is False
    assert repo.cursors[-1]["cursor"] == "attempts:1;no_job"


async def test_kupo_done_marker_at_cap_skips_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _RecordingRepo(cursor={"source": "kupo", "done": 1, "txs_seen": 500})
    flavor, requests = _kupo(monkeypatch, repo, lambda r: httpx.Response(500, json={}))
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "skipped" and out.txs_ingested == 500
    assert requests == []


async def test_kupo_raised_cap_reopens_completed_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # done at txs_seen below the RAISED cap must re-run (parity with blockfrost).
    repo = _RecordingRepo(cursor={"source": "kupo", "done": 1, "txs_seen": 500})
    flavor, requests = _kupo(monkeypatch, repo, lambda r: httpx.Response(202, json={}))
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=1000, progress=lambda _m: None
    )
    assert out.status == "pending"
    assert [r.method for r in requests] == ["POST"]


async def test_kupo_status_poll_flips_cursor_done_on_our_complete_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An outstanding marker (done=0) checks the host job; OUR complete job
    # (created_before_slot present, result.complete) flips the marker.
    repo = _RecordingRepo(
        cursor={"source": "kupo", "done": 0, "txs_seen": 0, "cursor": "attempts:1"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "status": "done",
                "created_before_slot": 12345,
                "result": {"txs_ingested": 42, "complete": True},
            },
        )

    flavor, requests = _kupo(monkeypatch, repo, handler)
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "completed" and out.txs_ingested == 42
    # txs_seen settled at the CAP (bounded history exhausted), not the raw count.
    assert repo.cursors[-1]["done"] is True and repo.cursors[-1]["txs_seen"] == 500
    assert len(requests) == 1


async def test_kupo_foreign_done_job_not_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    # A finished job with NO created_before_slot is an operator's manual
    # backfill: adopting it would freeze our bounded history as complete. It
    # must be ignored and our own bounded job triggered instead.
    repo = _RecordingRepo(
        cursor={"source": "kupo", "done": 0, "txs_seen": 0, "cursor": "attempts:1"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"status": "done", "created_before_slot": None, "result": {"complete": True}},
            )
        return httpx.Response(202, json={})

    flavor, requests = _kupo(monkeypatch, repo, handler)
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "pending"
    assert [r.method for r in requests] == ["GET", "POST"]


async def test_kupo_degraded_done_job_retriggers(monkeypatch: pytest.MonkeyPatch) -> None:
    # OUR job finished DEGRADED (result.complete False): re-trigger rather than
    # freeze a partial history as complete.
    repo = _RecordingRepo(
        cursor={"source": "kupo", "done": 0, "txs_seen": 0, "cursor": "attempts:1"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "status": "done",
                    "created_before_slot": 12345,
                    "result": {"complete": False, "degraded_reason": "blocks skipped"},
                },
            )
        return httpx.Response(202, json={})

    flavor, requests = _kupo(monkeypatch, repo, handler)
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "pending"
    assert [r.method for r in requests] == ["GET", "POST"]


async def test_kupo_gives_up_after_max_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    # A persistently failing backfill must not re-scan the host forever: after
    # _KUPO_MAX_TRIGGERS attempts it settles done (skip) with no further HTTP,
    # and the marker carries the gave-up flag so history_status says "failed"
    # instead of presenting the settled marker as a landed backfill.
    from app.service.history import _KUPO_MAX_TRIGGERS

    repo = _RecordingRepo(
        cursor={
            "source": "kupo",
            "done": 0,
            "txs_seen": 0,
            "cursor": f"attempts:{_KUPO_MAX_TRIGGERS}",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # The status check returns "failed" → would re-trigger, but the cap stops it.
        return httpx.Response(200, json={"status": "failed"})

    flavor, requests = _kupo(monkeypatch, repo, handler)
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "skipped" and "giving up" in out.note
    assert [r.method for r in requests] == ["GET"]  # checked, but did NOT re-POST
    assert repo.cursors[-1]["done"] is True
    assert repo.cursors[-1]["cursor"] == f"attempts:{_KUPO_MAX_TRIGGERS};gave_up"


async def test_kupo_gave_up_marker_skips_with_distinct_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A settled gave-up marker at the same cap must not read as a landed
    # backfill: the skip carries the gave-up note (and zero txs), no HTTP.
    repo = _RecordingRepo(
        cursor={"source": "kupo", "done": 1, "txs_seen": 500, "cursor": "attempts:3;gave_up"}
    )
    flavor, requests = _kupo(monkeypatch, repo, lambda r: httpx.Response(500, json={}))
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "skipped" and "gave up" in out.note and out.txs_ingested == 0
    assert requests == []


async def test_kupo_raised_cap_after_give_up_gets_fresh_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Raising the cap re-opens a gave-up marker WITH a fresh trigger budget:
    # keeping the exhausted counter would re-close the question on the same
    # tick without ever re-POSTing.
    repo = _RecordingRepo(
        cursor={"source": "kupo", "done": 1, "txs_seen": 500, "cursor": "attempts:3;gave_up"}
    )
    flavor, requests = _kupo(monkeypatch, repo, lambda r: httpx.Response(202, json={}))
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=1000, progress=lambda _m: None
    )
    assert out.status == "pending"
    assert [r.method for r in requests] == ["POST"]
    assert repo.cursors[-1]["cursor"] == "attempts:1"  # fresh budget, flag cleared


async def test_kupo_lost_host_job_retriggers(monkeypatch: pytest.MonkeyPatch) -> None:
    # 404 on the status check (host restarted, in-memory job store) → re-POST.
    repo = _RecordingRepo(
        cursor={"source": "kupo", "done": 0, "txs_seen": 0, "cursor": "attempts:1"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "no job"})
        return httpx.Response(202, json={"status": "running"})

    flavor, requests = _kupo(monkeypatch, repo, handler)
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "pending"
    assert [r.method for r in requests] == ["GET", "POST"]


# --- history_incomplete / history_status ---------------------------------------------


def test_history_incomplete_states(monkeypatch: pytest.MonkeyPatch) -> None:
    def _with_cursor(cur: dict[str, Any] | None, cap: int = 500) -> bool:
        _patch_repo(monkeypatch, _RecordingRepo(cursor=cur))
        return history_incomplete(_BF_SETTINGS, "addr1demo", cap)

    assert _with_cursor(None) is True  # deferred attempts write no marker
    assert _with_cursor({"source": "blockfrost_history", "done": 0, "txs_seen": 10}) is True
    assert _with_cursor({"source": "blockfrost_history", "done": 1, "txs_seen": 500}) is False
    # The ingester's max_reached terminal state (done=0 at the cap) is complete
    # AT that cap — the classify tick must stop resuming it…
    assert _with_cursor({"source": "blockfrost_history", "done": 0, "txs_seen": 500}) is False
    # …while a raised cap re-opens it automatically on the next tick.
    assert (
        _with_cursor({"source": "blockfrost_history", "done": 0, "txs_seen": 500}, cap=1000) is True
    )
    assert _with_cursor({"source": "kupo", "done": 1, "txs_seen": 42}) is False
    # A settled gave-up marker is NOT outstanding: only a raised cap retries it.
    assert (
        _with_cursor({"source": "kupo", "done": 1, "txs_seen": 500, "cursor": "attempts:3;gave_up"})
        is False
    )
    assert _with_cursor({"source": "host_ch", "done": 1, "txs_seen": 9}) is True


def test_history_status_states(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.service.history import history_status

    def _with_cursor(cur: dict[str, Any] | None, cap: int = 500) -> str:
        _patch_repo(monkeypatch, _RecordingRepo(cursor=cur))
        return history_status(_BF_SETTINGS, "addr1demo", cap)

    assert _with_cursor(None) == "none"
    assert (
        _with_cursor({"source": "blockfrost_history", "done": 0, "txs_seen": 10}) == "in_progress"
    )
    assert _with_cursor({"source": "blockfrost_history", "done": 1, "txs_seen": 500}) == "complete"
    # max_reached at the cap reads "complete", not a forever "in_progress".
    assert _with_cursor({"source": "blockfrost_history", "done": 0, "txs_seen": 500}) == "complete"
    assert (
        _with_cursor({"source": "blockfrost_history", "done": 0, "txs_seen": 500}, cap=1000)
        == "in_progress"
    )
    # The kupo give-up is reported as failed, not as a landed backfill.
    assert (
        _with_cursor({"source": "kupo", "done": 1, "txs_seen": 500, "cursor": "attempts:3;gave_up"})
        == "failed"
    )
    assert _with_cursor({"source": "host_ch", "done": 1, "txs_seen": 9}) == "none"
    # Feature disabled: always "none", no repo constructed.
    assert history_status(Settings(CHAIN_SOURCE="host_ch"), "addr1demo", 500) == "none"


# --- the never-raise contract ---------------------------------------------------------


async def test_blockfrost_run_never_raises_on_boundary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ClickHouse hiccup in the boundary aggregates must degrade to a deferred
    # attempt: the onboarding/classify job carrying this stage must not fail.
    _patch_repo(monkeypatch, _RecordingRepo())

    def _boom(settings: Any, target: str) -> HostBoundary:
        raise RuntimeError("simulated ClickHouse failure")

    monkeypatch.setattr("app.service.history.host_history_boundary", _boom)
    out = await BlockfrostHistory(_BF_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "deferred"


async def test_kupo_run_never_raises_on_cursor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenRepo(_RecordingRepo):
        def get_cursor(self, target: str) -> dict[str, Any] | None:
            raise RuntimeError("simulated ClickHouse failure")

    _patch_repo(monkeypatch, _BrokenRepo())
    out = await KupoHistory(_KUPO_SETTINGS).run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "deferred"


async def test_kupo_non_json_status_reply_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 200 that is not JSON (a proxy error page) is an infrastructure answer,
    # not a job verdict: keep waiting instead of raising or re-triggering.
    repo = _RecordingRepo(
        cursor={"source": "kupo", "done": 0, "txs_seen": 0, "cursor": "attempts:1"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Bad Gateway</html>")

    flavor, requests = _kupo(monkeypatch, repo, handler)
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "pending" and "non-JSON" in out.note
    assert [r.method for r in requests] == ["GET"]
    assert repo.cursors == []  # marker untouched: nothing was learned


async def test_kupo_auth_failure_defers_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A wrong HOST_API_KEY passes the startup guard (presence, not validity):
    # the trigger loop must say so in the logs instead of churning invisibly.
    repo = _RecordingRepo()
    flavor, _ = _kupo(monkeypatch, repo, lambda r: httpx.Response(401, json={"detail": "bad key"}))
    with caplog.at_level("WARNING", logger="app.service.history"):
        out = await flavor.run(
            target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
        )
    assert out.status == "deferred" and "auth" in out.note
    # OUR job never started, but a persistently wrong key must still spend an
    # attempt (no_job-marked) so it eventually crosses _KUPO_MAX_TRIGGERS
    # instead of retrying forever with no give-up signal.
    assert repo.cursors[-1]["done"] is False
    assert repo.cursors[-1]["cursor"] == "attempts:1;no_job"
    assert any("HOST_API_KEY" in r.message for r in caplog.records)


async def test_kupo_gives_up_after_repeated_no_job_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A persistently wrong HOST_API_KEY (or unreachable host, or missing
    # KUPO_URL) must eventually give up loudly, exactly like a persistently
    # failing/degraded HOST JOB already does (test_kupo_gives_up_after_max_triggers):
    # otherwise a pure misconfiguration retries forever with no operator signal.
    from app.service.history import _KUPO_MAX_TRIGGERS

    repo = _RecordingRepo(
        cursor={
            "source": "kupo",
            "done": False,
            "txs_seen": 0,
            "cursor": f"attempts:{_KUPO_MAX_TRIGGERS - 1};no_job",
        }
    )
    flavor, _requests = _kupo(
        monkeypatch, repo, lambda r: httpx.Response(401, json={"detail": "bad key"})
    )
    out = await flavor.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out.status == "deferred" and "auth" in out.note
    assert repo.cursors[-1]["cursor"] == f"attempts:{_KUPO_MAX_TRIGGERS};no_job"

    # One more tick: the attempts wall now engages BEFORE any HTTP call.
    repo2 = _RecordingRepo(
        cursor={
            "source": "kupo",
            "done": False,
            "txs_seen": 0,
            "cursor": f"attempts:{_KUPO_MAX_TRIGGERS};no_job",
        }
    )
    flavor2, requests2 = _kupo(monkeypatch, repo2, lambda r: httpx.Response(500, json={}))
    out2 = await flavor2.run(
        target="addr1demo", target_type="address", max_txs=500, progress=lambda _m: None
    )
    assert out2.status == "skipped" and "giving up" in out2.note
    assert requests2 == []  # gave up before ever calling the host
    assert repo2.cursors[-1]["done"] is True
    assert repo2.cursors[-1]["cursor"] == f"attempts:{_KUPO_MAX_TRIGGERS};gave_up"
