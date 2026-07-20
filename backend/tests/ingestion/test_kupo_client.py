"""Unit tests for the Kupo client (the address→tx index used by backfill).

Kupo's HTTP responses are stubbed with ``httpx.MockTransport`` using fixtures
shaped exactly like real Kupo v2.11 output (verified live against preprod):
``/matches`` records carry ``transaction_id`` + ``created_at`` and, when spent,
``spent_at`` with its own ``transaction_id``; ``/checkpoints/{slot}`` returns the
nearest ``{slot_no, header_hash}`` at-or-before the slot, or ``null``.
"""

from __future__ import annotations

import httpx
import pytest

from app.ingestion.kupo_client import (
    ChainPoint,
    KupoClient,
    KupoError,
    KupoUnavailable,
    TxPoint,
)

_BASE = "http://kupo.test:1442"


def _created(slot: int, header: str) -> dict:
    return {"slot_no": slot, "header_hash": header}


def _match(tx: str, slot: int, header: str, *, spent: dict | None = None) -> dict:
    m = {
        "transaction_id": tx,
        "output_index": 0,
        "address": "addr_test1xyz",
        "value": {"coins": 1_000_000, "assets": {}},
        "created_at": _created(slot, header),
        "spent_at": spent,
    }
    return m


def _client(handler) -> KupoClient:
    return KupoClient(_BASE, transport=httpx.MockTransport(handler))


async def test_missing_url_raises_unavailable() -> None:
    with pytest.raises(KupoUnavailable):
        KupoClient("")


async def test_address_tx_points_unions_created_and_spent() -> None:
    # One output created by tx AA in block 100, later spent by tx BB in block 120.
    matches = [
        _match(
            "aa",
            100,
            "h100",
            spent={"slot_no": 120, "header_hash": "h120", "transaction_id": "bb"},
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/matches/addr_test1xyz"
        assert request.url.params.get("order") == "most_recent_first"
        return httpx.Response(200, json=matches)

    points = await _client(handler).address_tx_points("addr_test1xyz")
    # Both the creating and the spending transaction are surfaced.
    assert {p.tx_hash for p in points} == {"aa", "bb"}
    assert TxPoint("bb", 120, "h120") in points
    assert TxPoint("aa", 100, "h100") in points


async def test_newest_first_and_max_txs_cap() -> None:
    matches = [
        _match("aa", 100, "h100"),
        _match("bb", 300, "h300"),
        _match("cc", 200, "h200"),
        _match("aa", 100, "h100"),  # duplicate hash → deduped, first point wins
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=matches)

    client = _client(handler)
    all_points = await client.address_tx_points("addr_test1xyz")
    assert [p.tx_hash for p in all_points] == ["bb", "cc", "aa"]  # slot desc

    latest_two = await client.address_tx_points("addr_test1xyz", max_txs=2)
    assert [p.tx_hash for p in latest_two] == ["bb", "cc"]


async def test_partial_point_is_ignored() -> None:
    # A match whose created_at lacks header_hash is unusable and dropped.
    matches = [
        {"transaction_id": "aa", "created_at": {"slot_no": 100}},
        _match("bb", 200, "h200"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=matches)

    points = await _client(handler).address_tx_points("addr_test1xyz")
    assert [p.tx_hash for p in points] == ["bb"]


async def test_ancestor_point_asks_one_slot_before() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"slot_no": 995, "header_hash": "anc"})

    point = await _client(handler).ancestor_point(1000)
    # Must query strictly before the target block so a forward walk re-covers it.
    assert seen["path"] == "/checkpoints/999"
    assert point == ChainPoint(995, "anc")


async def test_ancestor_point_none_when_no_checkpoint() -> None:
    # Kupo returns the literal JSON ``null`` (verified live) when it has no
    # checkpoint that far back, not an empty body.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})

    assert await _client(handler).ancestor_point(1000) is None


async def test_ancestor_point_origin_guard() -> None:
    # No HTTP call for a non-positive slot; there is nothing before origin.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not call Kupo for before_slot <= 0")

    assert await _client(handler).ancestor_point(0) is None


async def test_http_error_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(KupoError):
        await _client(handler).address_tx_points("addr_test1xyz")


async def test_health_returns_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(
            200,
            json={
                "connection_status": "connected",
                "most_recent_checkpoint": 128449934,
                "most_recent_node_tip": 128449934,
            },
        )

    health = await _client(handler).health()
    assert health["connection_status"] == "connected"


async def test_created_before_forwarded_and_strictly_filtered() -> None:
    # The slot bound is forwarded to Kupo as created_before AND enforced
    # client-side: a pre-boundary output SPENT post-boundary still surfaces its
    # spending tx in the response (Kupo filters on match creation), and that
    # point must not leak past the strict bound.
    matches = [
        _match(
            "aa",
            100,
            "h100",
            spent={"slot_no": 120, "header_hash": "h120", "transaction_id": "bb"},
        ),
        _match("cc", 90, "h90"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("created_before") == "110"
        return httpx.Response(200, json=matches)

    points = await _client(handler).address_tx_points("addr_test1xyz", created_before_slot=110)
    # bb (slot 120) is dropped by the client-side strict filter.
    assert [p.tx_hash for p in points] == ["aa", "cc"]
