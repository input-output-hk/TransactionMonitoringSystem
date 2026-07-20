"""Unit tests for the on-demand address backfill.

The Ogmios chain-sync session is stubbed with a scripted fake WebSocket that
mirrors the real protocol: after ``findIntersection`` the first ``nextBlock`` is
a RollBackward to the intersection point, then blocks roll forward. Kupo is
stubbed to point at specific block slots (and to report a healthy, caught-up
index). ClickHouse insert, the raw store, and cross-block input resolution are
patched (no DB, no filesystem), and ``parse_ogmios_transaction`` runs for real so
the records are genuine.
"""

from __future__ import annotations

import json

import pytest

import app.ingestion.address_backfill as ab
from app.ingestion.address_backfill import (
    BackfillError,
    _ingest_block_targets,
    backfill_address,
)
from app.ingestion.chain_time import SlotTimeConverter
from app.ingestion.kupo_client import ChainPoint, TxPoint

# 64-hex tx ids (the parser uses tx_data["id"] verbatim as the hash).
AA = "aa" * 32
BB = "bb" * 32
CC = "cc" * 32
OTHER = "0f" * 32

# Byron-shaped era fixture (20 s slots from genesis) so slot→time is exact.
_SYSTEM_START = "2022-06-01T00:00:00Z"
_ERAS = [
    {
        "start": {"time": {"seconds": 0}, "slot": 0, "epoch": 0},
        "parameters": {"epochLength": 21_600, "slotLength": {"milliseconds": 20_000}},
    }
]

# A healthy, caught-up Kupo (checkpoint well past any target slot below).
_HEALTHY = {"connection_status": "connected", "most_recent_checkpoint": 10_000_000}


def _tx(tx_id: str) -> dict:
    return {
        "id": tx_id,
        "spends": "inputs",
        "fee": {"ada": {"lovelace": 200_000}},
        "inputs": [{"transaction": {"id": "11" * 32}, "index": 0}],
        "outputs": [{"address": "addr_test1qq", "value": {"ada": {"lovelace": 1_000_000}}}],
    }


def _block(slot: int, tx_ids: list[str]) -> dict:
    return {"id": "ab" * 32, "slot": slot, "height": slot, "transactions": [_tx(t) for t in tx_ids]}


class _FakeWS:
    """Responds to the reader's JSON-RPC by method. ``script`` is the ordered
    ``nextBlock`` outcomes as ``("forward", block)`` / ``("backward", point)``;
    once exhausted it returns a far-future empty block so a walk that is still
    searching terminates via the slot>latest guard instead of hanging.

    ``intersection_error=True`` makes ``findIntersection`` return a JSON-RPC error
    so the reader raises ``BackfillError``."""

    def __init__(
        self,
        script: list[tuple[str, dict]],
        *,
        with_converter: bool,
        intersection_error: bool = False,
    ) -> None:
        self._script = script
        self._i = 0
        self._with_converter = with_converter
        self._intersection_error = intersection_error
        self._method = ""

    async def send(self, msg: str) -> None:
        self._method = json.loads(msg)["method"]

    async def recv(self) -> str:
        tip = {"slot": 10_000_000, "id": "ff" * 32}
        if self._method == "queryNetwork/startTime":
            return json.dumps({"result": _SYSTEM_START if self._with_converter else None})
        if self._method == "queryLedgerState/eraSummaries":
            return json.dumps({"result": _ERAS if self._with_converter else None})
        if self._method == "findIntersection":
            if self._intersection_error:
                return json.dumps({"error": {"code": -32000, "message": "no intersection"}})
            return json.dumps({"result": {"intersection": {"slot": 90}, "tip": tip}})
        # nextBlock
        if self._i < len(self._script):
            direction, payload = self._script[self._i]
            self._i += 1
            key = "block" if direction == "forward" else "point"
            return json.dumps({"result": {"direction": direction, key: payload, "tip": tip}})
        far = _block(10_000_000, [])
        return json.dumps({"result": {"direction": "forward", "block": far, "tip": tip}})


class _StubKupo:
    points: list[TxPoint] = []
    ancestor: ChainPoint | None = None
    health_data: dict = dict(_HEALTHY)

    def __init__(self, *_a, **_k) -> None:
        pass

    async def address_tx_points(
        self,
        address: str,
        *,
        max_txs: int | None = None,
        created_before_slot: int | None = None,
    ) -> list[TxPoint]:
        # Mirror the real client: newest-first, bound, then cap, so a test can
        # assert both flow through backfill_address.
        pts = sorted(_StubKupo.points, key=lambda p: (p.slot, p.tx_hash), reverse=True)
        if created_before_slot is not None:
            pts = [p for p in pts if p.slot < created_before_slot]
        if max_txs is not None:
            pts = pts[:max_txs]
        return pts

    async def ancestor_point(self, before_slot: int) -> ChainPoint | None:
        return _StubKupo.ancestor

    async def health(self) -> dict:
        return dict(_StubKupo.health_data)


def _patch_common(
    monkeypatch,
    fake_ws: _FakeWS,
    inserted: list,
    *,
    raw_written: list | None = None,
    parse_failed: list | None = None,
) -> None:
    class _Conn:
        async def __aenter__(self):
            return fake_ws

        async def __aexit__(self, *_a):
            return False

    _StubKupo.health_data = dict(_HEALTHY)
    monkeypatch.setattr(ab, "KupoClient", _StubKupo)
    monkeypatch.setattr(ab.websockets, "connect", lambda *_a, **_k: _Conn())

    async def _fake_insert(txs):
        inserted.extend(txs)

    async def _identity_resolve(txs, network):
        return txs

    async def _fake_write_confirmed(network, tx_hash, raw_data, ts):
        if raw_written is not None:
            raw_written.append(tx_hash)

    async def _fake_write_parse_failed(network, tx_id, tx_data, ts):
        if parse_failed is not None:
            parse_failed.append(tx_id)

    monkeypatch.setattr(ab.clickhouse, "insert_transactions_batch_async", _fake_insert)
    monkeypatch.setattr(ab, "resolve_input_amounts", _identity_resolve)
    monkeypatch.setattr(ab.raw_store, "write_confirmed", _fake_write_confirmed)
    monkeypatch.setattr(ab.raw_store, "write_parse_failed", _fake_write_parse_failed)


async def test_backfill_scans_targets_stops_and_reports_missing(monkeypatch) -> None:
    _StubKupo.points = [
        TxPoint(AA, 100, "h100"),
        TxPoint(BB, 120, "h120"),
        TxPoint(CC, 120, "h120"),  # never appears in a block → reported missing
    ]
    _StubKupo.ancestor = ChainPoint(90, "h90")
    script = [
        ("backward", {"slot": 90, "id": "h90"}),  # protocol: RollBackward to intersection
        ("forward", _block(100, [OTHER, AA])),  # AA at block_index 1
        ("forward", _block(120, [BB])),
        ("forward", _block(130, [])),  # slot > latest(120) → stop
    ]
    inserted: list = []
    _patch_common(monkeypatch, _FakeWS(script, with_converter=True), inserted)

    result = await backfill_address(AA, network="preprod", max_txs=None)

    assert {tx.tx_hash for tx in inserted} == {AA, BB}
    assert result.requested_txs == 3
    assert result.txs_ingested == 2
    assert result.blocks_scanned == 2  # blocks 100 and 120 (130 breaks before counting)
    assert result.missing_tx_hashes == [CC]
    assert result.complete is True
    assert result.degraded_reason is None


async def test_backfill_writes_raw_store(monkeypatch) -> None:
    _StubKupo.points = [TxPoint(AA, 100, "h100")]
    _StubKupo.ancestor = ChainPoint(90, "h90")
    script = [
        ("backward", {"slot": 90, "id": "h90"}),
        ("forward", _block(100, [AA])),
        ("forward", _block(110, [])),  # > latest(100) → stop
    ]
    inserted: list = []
    raw_written: list = []
    _patch_common(
        monkeypatch, _FakeWS(script, with_converter=True), inserted, raw_written=raw_written
    )

    result = await backfill_address(AA, network="preprod")

    # The full payload is written to the raw store before the ClickHouse insert,
    # so the engine's oversized-tx fallback works for backfilled rows too.
    assert raw_written == [AA]
    assert result.txs_ingested == 1


async def test_backfill_caps_at_max_txs(monkeypatch) -> None:
    # Three matches, cap 2: only the two newest are targeted, the oldest (AA@100)
    # is never in `needed` and never fetched.
    _StubKupo.points = [
        TxPoint(AA, 100, "h100"),
        TxPoint(BB, 120, "h120"),
        TxPoint(CC, 140, "h140"),
    ]
    _StubKupo.ancestor = ChainPoint(110, "h110")
    script = [
        ("backward", {"slot": 110, "id": "h110"}),
        ("forward", _block(120, [BB])),
        ("forward", _block(140, [CC])),
        ("forward", _block(150, [])),  # > latest(140) → stop
    ]
    inserted: list = []
    _patch_common(monkeypatch, _FakeWS(script, with_converter=True), inserted)

    result = await backfill_address(AA, network="preprod", max_txs=2)

    assert result.requested_txs == 2  # cap applied
    assert {tx.tx_hash for tx in inserted} == {BB, CC}
    assert AA not in {tx.tx_hash for tx in inserted}
    assert result.missing_tx_hashes == []


async def test_backfill_forwards_created_before_slot_and_excludes_at_or_above(
    monkeypatch,
) -> None:
    # End-to-end: the orchestration call (backfill_address -> KupoClient) must
    # actually pass created_before_slot through, not just the lower-level
    # client filter it wraps (that layer is tested separately in
    # test_kupo_client.py). AA sits at the boundary itself and must be
    # excluded (strictly-below semantics), BB is below it and must survive.
    _StubKupo.points = [
        TxPoint(AA, 100, "h100"),  # at the boundary: excluded
        TxPoint(BB, 90, "h90"),  # below the boundary: included
    ]
    _StubKupo.ancestor = ChainPoint(80, "h80")
    script = [
        ("backward", {"slot": 80, "id": "h80"}),
        ("forward", _block(90, [BB])),
        ("forward", _block(100, [])),  # > latest(90) → stop
    ]
    inserted: list = []
    _patch_common(monkeypatch, _FakeWS(script, with_converter=True), inserted)

    result = await backfill_address(AA, network="preprod", created_before_slot=100)

    assert result.requested_txs == 1  # only BB passed the created_before_slot filter
    assert {tx.tx_hash for tx in inserted} == {BB}
    assert AA not in {tx.tx_hash for tx in inserted}


async def test_backfill_anchor_none_flags_degraded_and_skips_earliest(monkeypatch) -> None:
    # No pre-earliest checkpoint: the walk intersects AT the earliest target, so
    # forward delivery starts at its successor and the earliest block is skipped.
    _StubKupo.points = [TxPoint(AA, 100, "h100"), TxPoint(BB, 120, "h120")]
    _StubKupo.ancestor = None
    script = [
        ("backward", {"slot": 100, "id": "h100"}),  # rollback to the intersection point
        ("forward", _block(120, [BB])),
        ("forward", _block(130, [])),  # > latest(120) → stop
    ]
    inserted: list = []
    _patch_common(monkeypatch, _FakeWS(script, with_converter=True), inserted)

    result = await backfill_address(AA, network="preprod")

    assert {tx.tx_hash for tx in inserted} == {BB}
    assert result.missing_tx_hashes == [AA]  # earliest block skipped
    assert result.complete is False
    assert "earliest block may be skipped" in result.degraded_reason


async def test_backfill_flags_degraded_when_kupo_behind(monkeypatch) -> None:
    _StubKupo.points = [TxPoint(AA, 100, "h100")]
    _StubKupo.ancestor = ChainPoint(90, "h90")
    script = [
        ("backward", {"slot": 90, "id": "h90"}),
        ("forward", _block(100, [AA])),
        ("forward", _block(110, [])),
    ]
    inserted: list = []
    _patch_common(monkeypatch, _FakeWS(script, with_converter=True), inserted)
    # Kupo has only indexed to slot 50, behind the newest target (100).
    _StubKupo.health_data = {"connection_status": "connected", "most_recent_checkpoint": 50}

    result = await backfill_address(AA, network="preprod")

    assert result.txs_ingested == 1
    assert result.complete is False
    assert "indexed only to slot 50" in result.degraded_reason


async def test_backfill_contains_unparseable_target(monkeypatch) -> None:
    # One target tx fails to parse: it must not abort the whole run; the good tx
    # still ingests, the bad one is preserved and reported missing.
    _StubKupo.points = [TxPoint(AA, 100, "h100"), TxPoint(BB, 100, "h100")]
    _StubKupo.ancestor = ChainPoint(90, "h90")
    script = [
        ("backward", {"slot": 90, "id": "h90"}),
        ("forward", _block(100, [AA, BB])),
        ("forward", _block(110, [])),  # > latest(100) → stop
    ]
    inserted: list = []
    parse_failed: list = []
    _patch_common(
        monkeypatch, _FakeWS(script, with_converter=True), inserted, parse_failed=parse_failed
    )

    real_parse = ab.parse_ogmios_transaction

    def _flaky_parse(tx_data, **kwargs):
        if tx_data.get("id") == BB:
            raise ValueError("simulated parser choke")
        return real_parse(tx_data, **kwargs)

    monkeypatch.setattr(ab, "parse_ogmios_transaction", _flaky_parse)

    result = await backfill_address(AA, network="preprod")

    assert {tx.tx_hash for tx in inserted} == {AA}
    assert result.txs_ingested == 1
    assert result.missing_tx_hashes == [BB]  # not ingested, reported missing
    assert parse_failed == [BB]  # raw payload preserved for replay


async def test_backfill_raises_on_intersection_error(monkeypatch) -> None:
    _StubKupo.points = [TxPoint(AA, 100, "h100")]
    _StubKupo.ancestor = ChainPoint(90, "h90")
    inserted: list = []
    _patch_common(
        monkeypatch,
        _FakeWS([], with_converter=True, intersection_error=True),
        inserted,
    )

    with pytest.raises(BackfillError):
        await backfill_address(AA, network="preprod")


async def test_backfill_empty_when_kupo_has_no_matches(monkeypatch) -> None:
    _StubKupo.points = []
    _StubKupo.ancestor = None
    inserted: list = []
    _patch_common(monkeypatch, _FakeWS([], with_converter=False), inserted)

    result = await backfill_address(AA, network="preprod")
    assert result == ab.BackfillResult(AA, 0, 0, 0, [])
    assert inserted == []


async def test_ingest_block_targets_stamps_chain_time_and_preserves_index(monkeypatch) -> None:
    inserted: list = []
    raw_written: list = []

    async def _fake_insert(txs):
        inserted.extend(txs)

    async def _identity_resolve(txs, network):
        return txs

    async def _fake_write_confirmed(network, tx_hash, raw_data, ts):
        raw_written.append(tx_hash)

    monkeypatch.setattr(ab.clickhouse, "insert_transactions_batch_async", _fake_insert)
    monkeypatch.setattr(ab, "resolve_input_amounts", _identity_resolve)
    monkeypatch.setattr(ab.raw_store, "write_confirmed", _fake_write_confirmed)

    converter = SlotTimeConverter.from_ogmios(_SYSTEM_START, _ERAS)
    assert converter is not None
    slot = 100
    block = _block(slot, [OTHER, AA])  # target AA sits at block_index 1
    seen: set[str] = set()

    count = await _ingest_block_targets(block, slot, {AA}, seen, "preprod", converter)

    assert count == 1
    assert seen == {AA}
    assert len(inserted) == 1
    assert raw_written == [AA]  # raw payload written before the insert
    tx = inserted[0]
    assert tx.tx_hash == AA
    assert tx.block_index == 1  # not renumbered by the target filter
    assert tx.network == "preprod"
    # Chain-time, not wall clock: slot 100 at 20 s/slot from system start.
    assert tx.timestamp == converter.slot_to_utc(slot)


async def test_kupo_unavailable_propagates(monkeypatch) -> None:
    # No KUPO_URL configured → KupoClient() raises KupoUnavailable, surfaced to caller.
    monkeypatch.setattr(ab.settings, "KUPO_URL", "")
    from app.ingestion.kupo_client import KupoUnavailable

    with pytest.raises(KupoUnavailable):
        await backfill_address(AA, network="preprod")
