"""Tests for the Blockfrost data-source adapter's metadata normalization.

A stub client serves canned Blockfrost JSON so ``fetch_contract_metadata`` is
pinned without a network, mirroring the captured-fixture style of test_client.
``BlockfrostSource`` then wraps it and translates not-found into ``SourceNotFound``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.blockfrost.client import BlockfrostNotFoundError
from app.blockfrost.source import (
    BlockfrostSource,
    _page_cursor,
    _parse_cursor,
    fetch_contract_metadata,
)
from app.config import Settings
from app.sources.base import SourceNotFound

POLICY = "ab" * 28  # 56-hex policy id
UNIT_A = POLICY + "546f6b656e41"  # hex("TokenA")
UNIT_B = POLICY + "546f6b656e42"  # hex("TokenB")


class StubClient:
    """Minimal stand-in for AsyncBlockfrostClient exposing only what metadata needs."""

    def __init__(
        self,
        *,
        address: dict[str, Any] | None = None,
        script_info: dict[str, Any] | None = None,
        assets: dict[str, dict[str, Any]] | None = None,
        policy_pages: list[list[dict[str, Any]]] | None = None,
        missing: tuple[str, ...] = (),
    ) -> None:
        self._address = address
        self._script = script_info
        self._assets = assets or {}
        self._policy_pages = policy_pages or []
        self._missing = missing

    async def address_info(self, address: str) -> dict[str, Any]:
        if "address" in self._missing:
            raise BlockfrostNotFoundError("404")
        assert self._address is not None
        return self._address

    async def script(self, script_hash: str) -> dict[str, Any]:
        if "script" in self._missing:
            raise BlockfrostNotFoundError("404")
        assert self._script is not None
        return self._script

    async def asset(self, unit: str) -> dict[str, Any]:
        if unit not in self._assets:
            raise BlockfrostNotFoundError("404")
        return self._assets[unit]

    async def policy_assets(self, policy_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        for page in self._policy_pages:
            yield page


async def test_fetch_address_metadata_with_tokens() -> None:
    client = StubClient(
        address={
            "address": "addr1x",
            "script": True,
            "amount": [
                {"unit": "lovelace", "quantity": "5000000"},
                {"unit": UNIT_A, "quantity": "1"},
                {"unit": UNIT_B, "quantity": "10"},
            ],
        },
        assets={
            UNIT_A: {
                "asset": UNIT_A,
                "asset_name": "546f6b656e41",
                "onchain_metadata": {"name": "Fancy Token A"},
            },
            UNIT_B: {"asset": UNIT_B, "asset_name": "546f6b656e42"},  # no metadata -> hex decode
        },
    )
    meta = await fetch_contract_metadata(client, "addr1x", "address")
    assert meta["exists"] == 1
    assert meta["is_script"] == 1
    assert meta["script_type"] == ""
    assert meta["balance_lovelace"] == 5_000_000
    assert meta["asset_count"] == 2
    tokens = json.loads(meta["sample_tokens"])
    names = {t["name"] for t in tokens}
    assert names == {"Fancy Token A", "TokenB"}
    assert all(t["policy_id"] == POLICY for t in tokens)


async def test_fetch_address_metadata_not_found() -> None:
    client = StubClient(missing=("address",))
    with pytest.raises(BlockfrostNotFoundError):
        await fetch_contract_metadata(client, "addrmissing", "address")


async def test_fetch_policy_metadata() -> None:
    client = StubClient(
        script_info={"script_hash": POLICY, "type": "plutusV2"},
        policy_pages=[[{"asset": UNIT_A}, {"asset": UNIT_B}]],
        assets={
            UNIT_A: {"asset": UNIT_A, "asset_name": "546f6b656e41"},
            UNIT_B: {"asset": UNIT_B, "asset_name": "546f6b656e42"},
        },
    )
    meta = await fetch_contract_metadata(client, POLICY, "policy")
    assert meta["exists"] == 1
    assert meta["is_script"] == 1
    assert meta["script_type"] == "plutusV2"
    assert meta["balance_lovelace"] == 0
    assert meta["asset_count"] == 2
    assert len(json.loads(meta["sample_tokens"])) == 2


async def test_fetch_policy_metadata_not_found() -> None:
    client = StubClient(missing=("script",))
    with pytest.raises(BlockfrostNotFoundError):
        await fetch_contract_metadata(client, POLICY, "policy")


async def test_source_metadata_translates_not_found() -> None:
    """BlockfrostSource.metadata wraps the provider's not-found into the neutral
    SourceNotFound the engine catches."""
    source = BlockfrostSource(
        Settings(BLOCKFROST_PROJECT_ID="t"), client=StubClient(missing=("address",))
    )
    with pytest.raises(SourceNotFound):
        await source.metadata("addrmissing", "address")


class _RaisingDiscoveryClient:
    """Client whose address tx-listing raises a provider error mid-discovery."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def address_transactions(self, address: str, **kwargs: Any) -> AsyncIterator[Any]:
        raise self._exc
        yield  # pragma: no cover - marks this an async generator


async def test_discovery_not_found_translated_to_source_error() -> None:
    """A provider error raised while *discovering* tx pages is translated to the
    neutral SourceError hierarchy too — not just per-tx fetch / metadata — so no
    provider exception escapes the adapter."""
    source = BlockfrostSource(
        Settings(BLOCKFROST_PROJECT_ID="t"),
        client=_RaisingDiscoveryClient(BlockfrostNotFoundError("404")),
    )
    with pytest.raises(SourceNotFound):
        async for _ in source.tx_hash_pages(
            address="addr1x",
            policy_id=None,
            cursor=None,
            mode="restart",
            max_items=None,
            from_block=None,
            to_block=None,
            progress=lambda _m: None,
        ):
            pass


# --- Cursor encoding ------------------------------------------------------------


def test_parse_cursor_round_trips_both_forms() -> None:
    assert _parse_cursor(_page_cursor(3)) == (3, None)
    assert _parse_cursor(_page_cursor(3, "10422911")) == (3, "10422911")
    assert _page_cursor(3, "10422911") == "page:3;from:10422911"


@pytest.mark.parametrize(
    "cursor",
    [
        None,
        "",
        "point:12.ab",
        "page:",
        "page:abc",
        "page:3;from:",
        "page:3;from:abc",
        "page:3;from:-5",
        "page:3;from:0",
        "page:3;from:³",
    ],
)
def test_parse_cursor_rejects_garbled_and_foreign_cursors(cursor: str | None) -> None:
    """Foreign/garbled cursors — INCLUDING a garbled anchor — parse to (0, None),
    i.e. restart. Degrading a broken anchor to (page, None) would continue at page
    N of the UNFILTERED set, a different position, and could skip data."""
    assert _parse_cursor(cursor) == (0, None)
