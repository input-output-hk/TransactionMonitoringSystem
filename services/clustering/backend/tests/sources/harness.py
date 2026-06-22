"""Conformance harness: the executable spec every ``ChainSource`` adapter must pass.

Wire fixtures can't be shared across providers (Blockfrost JSON vs a node's
CBOR), so the suite is *scenario-driven*: each adapter registers a factory that
builds a fresh source preloaded with the canonical scenario below, and
``test_conformance.py`` asserts purely in protocol terms (hashes, cursors,
``SourceError`` taxonomy, ``NormalizedTx`` consistency, ``TargetMeta`` keys).

Porting checklist for a new adapter (Kupo / db-sync / ...):
  1. Build a stubbed client serving the scenario's data in your wire format.
  2. Register a factory in ``SOURCE_FACTORIES`` returning (source, Scenario).
  3. Run ``pytest tests/sources`` — green means the engine can drive you.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.blockfrost.client import AsyncBlockfrostClient
from app.blockfrost.source import BlockfrostSource
from app.config import Settings
from app.sources.base import ChainSource


@dataclass(slots=True)
class Scenario:
    """What the canonical scenario contains, in protocol terms."""

    target: str
    target_type: str  # 'address' | 'policy'
    expected_hashes: list[str]  # full discovery result, in the source's stable order
    missing_tx: str  # fetch_tx must raise SourceNotFound
    rate_limited_target: str  # discovery on THIS target must raise SourceRateLimited
    # Expected hashes for window="recent" with max_items=len(recent_window).
    # None = the adapter ignores the hint (suite then asserts history behaviour).
    recent_window: list[str] | None = None


SourceFactory = Callable[[], tuple[ChainSource, Scenario]]


# --- Blockfrost scenario ------------------------------------------------------

# Five txs over three pages (page size 2) — enough to cut a resume anywhere.
# Distinct block heights so the recent-window pre-walk can anchor mid-history.
_HASHES = ["aa", "bb", "cc", "dd", "ee"]
_HEIGHTS = {h: (i + 1) * 10 for i, h in enumerate(_HASHES)}
_PAGE_SIZE = 2

_SCENARIO = Scenario(
    target="addr1conformance",
    target_type="address",
    expected_hashes=list(_HASHES),
    missing_tx="feedbead",
    rate_limited_target="addr1limited",
    recent_window=["cc", "dd", "ee"],
)


def _detail(tx_hash: str) -> dict[str, Any]:
    return {
        "hash": tx_hash, "block_height": 1, "block_time": 1_700_000_000, "slot": 1,
        "fees": "200000", "deposit": "0", "size": 300,
        "valid_contract": True, "redeemer_count": 0,
    }


def _utxos(tx_hash: str) -> dict[str, Any]:
    return {
        "hash": tx_hash,
        "inputs": [
            {"address": "addrIn", "amount": [{"unit": "lovelace", "quantity": "1000000"}]}
        ],
        "outputs": [
            {
                "address": "addrOut",
                "amount": [{"unit": "lovelace", "quantity": "900000"}],
                "output_index": 0,
            }
        ],
    }


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith(f"/addresses/{_SCENARIO.rate_limited_target}/transactions"):
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=[{"tx_hash": h} for h in _HASHES[:_PAGE_SIZE]])
        return httpx.Response(402, json={})  # daily limit mid-discovery
    if path.endswith("/transactions") and "/addresses/" in path:
        params = request.url.params
        hashes = list(_HASHES)
        if params.get("from") is not None:
            hashes = [h for h in hashes if _HEIGHTS[h] >= int(params["from"])]
        if params.get("order") == "desc":
            hashes = hashes[::-1]
        page = int(params.get("page", "1"))
        chunk = hashes[(page - 1) * _PAGE_SIZE : page * _PAGE_SIZE]
        return httpx.Response(
            200, json=[{"tx_hash": h, "block_height": _HEIGHTS[h]} for h in chunk]
        )
    if path.endswith("/utxos"):
        return httpx.Response(200, json=_utxos(path.split("/")[-2]))
    if "/txs/" in path:
        tx_hash = path.split("/")[-1]
        if tx_hash == _SCENARIO.missing_tx:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=_detail(tx_hash))
    if "/addresses/" in path:  # address_info for metadata()
        return httpx.Response(
            200,
            json={"amount": [{"unit": "lovelace", "quantity": "5000000"}], "script": True},
        )
    return httpx.Response(404, json={})


def _make_blockfrost() -> tuple[ChainSource, Scenario]:
    settings = Settings(BLOCKFROST_PROJECT_ID="t", BLOCKFROST_PAGE_SIZE=_PAGE_SIZE)
    inner = httpx.AsyncClient(
        base_url=settings.blockfrost_base_url,
        headers={"project_id": settings.blockfrost_project_id},
        transport=httpx.MockTransport(_handler),
    )
    return BlockfrostSource(settings, client=AsyncBlockfrostClient(settings, client=inner)), _SCENARIO


SOURCE_FACTORIES: dict[str, SourceFactory] = {
    "blockfrost": _make_blockfrost,
    # "kupo": _make_kupo,        <- future adapters register here
    # "dbsync": _make_dbsync,
}
