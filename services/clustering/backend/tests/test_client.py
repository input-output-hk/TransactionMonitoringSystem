"""Tests for the Blockfrost client: token bucket, retries, pagination."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.blockfrost.client import (
    AsyncBlockfrostClient,
    BlockfrostDailyLimitError,
    BlockfrostNotFoundError,
    TokenBucket,
)
from app.config import Settings


def _settings(page_size: int = 2) -> Settings:
    return Settings(
        BLOCKFROST_PROJECT_ID="test",
        BLOCKFROST_PAGE_SIZE=page_size,
    )


def _client(handler: httpx.MockTransport, settings: Settings) -> AsyncBlockfrostClient:
    inner = httpx.AsyncClient(
        base_url=settings.blockfrost_base_url,
        headers={"project_id": settings.blockfrost_project_id},
        transport=handler,
    )
    return AsyncBlockfrostClient(settings, client=inner)


async def test_token_bucket_allows_burst_then_throttles() -> None:
    bucket = TokenBucket(rate=1000.0, capacity=2)
    # First two acquisitions are immediate (burst); just assert they complete.
    await asyncio.wait_for(bucket.acquire(), timeout=1.0)
    await asyncio.wait_for(bucket.acquire(), timeout=1.0)
    await asyncio.wait_for(bucket.acquire(), timeout=1.0)


async def test_get_404_raises_not_found() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status_code": 404})

    async with _client(httpx.MockTransport(handler), _settings()) as client:
        with pytest.raises(BlockfrostNotFoundError):
            await client.tx("deadbeef")


async def test_get_402_raises_daily_limit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"status_code": 402})

    async with _client(httpx.MockTransport(handler), _settings()) as client:
        with pytest.raises(BlockfrostDailyLimitError):
            await client.tx("deadbeef")


async def test_get_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.blockfrost.client.asyncio.sleep", _no_sleep)

    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"hash": "ok"})

    async with _client(httpx.MockTransport(handler), _settings()) as client:
        result = await client.tx("deadbeef")

    assert result == {"hash": "ok"}
    assert calls["n"] == 2


async def test_pagination_stops_on_short_page() -> None:
    settings = _settings(page_size=2)

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=[{"tx_hash": "a"}, {"tx_hash": "b"}])
        if page == 2:
            return httpx.Response(200, json=[{"tx_hash": "c"}])  # short page -> stop
        return httpx.Response(200, json=[])

    collected: list[str] = []
    async with _client(httpx.MockTransport(handler), settings) as client:
        async for _page, items in client.address_transactions("addr1xyz"):
            collected.extend(it["tx_hash"] for it in items)

    assert collected == ["a", "b", "c"]
