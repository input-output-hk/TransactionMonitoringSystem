"""Unit tests for the on-demand address backfill.

The Ogmios chain-sync session is stubbed with a scripted fake WebSocket that
mirrors the real protocol: after ``findIntersection`` the first ``nextBlock`` is
a RollBackward to the intersection point, then blocks roll forward. Kupo is
stubbed to point at specific block slots. ClickHouse insert and cross-block input
resolution are patched (no DB), and ``parse_ogmios_transaction`` runs for real so
the records are genuine.
"""

from __future__ import annotations

import json

import pytest

import app.ingestion.address_backfill as ab
from app.ingestion.address_backfill import _ingest_block_targets, backfill_address
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
    searching terminates via the slot>latest guard instead of hanging."""

    def __init__(self, script: list[tuple[str, dict]], *, with_converter: bool) -> None:
        self._script = script
        self._i = 0
        self._with_converter = with_converter
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

    def __init__(self, *_a, **_k) -> None:
        pass

    async def address_tx_points(self, address: str, *, max_txs: int | None = None) -> list[TxPoint]:
        return list(_StubKupo.points)

    async def ancestor_point(self, before_slot: int) -> ChainPoint | None:
        return _StubKupo.ancestor


def _patch_common(monkeypatch, fake_ws: _FakeWS, inserted: list) -> None:
    class _Conn:
        async def __aenter__(self):
            return fake_ws

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(ab, "KupoClient", _StubKupo)
    monkeypatch.setattr(ab.websockets, "connect", lambda *_a, **_k: _Conn())

    async def _fake_insert(txs):
        inserted.extend(txs)

    async def _identity_resolve(txs, network):
        return txs

    monkeypatch.setattr(ab.clickhouse, "insert_transactions_batch_async", _fake_insert)
    monkeypatch.setattr(ab, "resolve_input_amounts", _identity_resolve)


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

    async def _fake_insert(txs):
        inserted.extend(txs)

    async def _identity_resolve(txs, network):
        return txs

    monkeypatch.setattr(ab.clickhouse, "insert_transactions_batch_async", _fake_insert)
    monkeypatch.setattr(ab, "resolve_input_amounts", _identity_resolve)

    converter = SlotTimeConverter.from_ogmios(_SYSTEM_START, _ERAS)
    assert converter is not None
    slot = 100
    block = _block(slot, [OTHER, AA])  # target AA sits at block_index 1
    seen: set[str] = set()

    count = await _ingest_block_targets(block, slot, {AA}, seen, "preprod", converter)

    assert count == 1
    assert seen == {AA}
    assert len(inserted) == 1
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
