"""Async Blockfrost client with rate limiting, pagination and retry/backoff.

Blockfrost free-tier limits (as documented): 10 requests/second sustained with a
burst of 500 that refills at 10/s. HTTP 429 is returned when the rate is
exceeded and HTTP 402 when the *daily* request cap is hit. We rate-limit
proactively with a token bucket, retry transient errors with exponential
backoff, and surface 402 as a dedicated exception so callers can stop and
resume later.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import Settings, get_settings


class BlockfrostError(RuntimeError):
    """Base error for Blockfrost responses."""


class BlockfrostNotFoundError(BlockfrostError):
    """Raised on HTTP 404 (unknown address/asset/tx)."""


class BlockfrostDailyLimitError(BlockfrostError):
    """Raised on HTTP 402 — the project's daily request limit was reached."""


class TokenBucket:
    """A simple asyncio token bucket.

    `rate` tokens are added per second up to `capacity`. `acquire()` blocks
    until a token is available. Acquisition is serialized by a lock so callers
    are admitted in order.
    """

    def __init__(self, rate: float, capacity: int) -> None:
        self._rate = rate
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


class AsyncBlockfrostClient:
    """Minimal async client over the Blockfrost Cardano API."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._bucket = TokenBucket(
            rate=self._settings.blockfrost_max_rps,
            capacity=self._settings.blockfrost_burst,
        )
        self._max_retries = (
            max_retries if max_retries is not None else self._settings.blockfrost_max_retries
        )
        self._backoff_cap = self._settings.blockfrost_backoff_cap_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.blockfrost_base_url,
            headers={"project_id": self._settings.blockfrost_project_id},
            timeout=httpx.Timeout(self._settings.blockfrost_timeout_s),
        )

    async def __aenter__(self) -> AsyncBlockfrostClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _retry_delay(self, resp: httpx.Response, backoff: float) -> float:
        """Seconds to wait before retrying a 429/5xx response.

        Honors the server's ``Retry-After`` hint when present, clamped to
        ``[0, backoff_cap]``: a hostile or misconfigured upstream must not park a
        worker for hours, a negative value must not crash ``asyncio.sleep``, and a
        NaN survives ``min()`` but ``max(0.0, nan)`` returns 0.0 so it can't reach
        sleep either. An HTTP-date hint (not parseable as seconds) and the no-hint
        case fall back to full-jitter exponential backoff, so concurrent fetchers
        don't retry in lockstep."""
        retry_after = resp.headers.get("Retry-After")
        if not retry_after:
            return random.uniform(0.0, backoff)
        try:
            delay = float(retry_after)
        except ValueError:
            delay = backoff
        return max(0.0, min(delay, self._backoff_cap))

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a path, returning parsed JSON. Handles rate limiting and retries."""
        backoff = 1.0
        for attempt in range(self._max_retries + 1):
            await self._bucket.acquire()
            try:
                resp = await self._client.get(path, params=params)
            except httpx.TransportError:
                if attempt >= self._max_retries:
                    raise
                # Full jitter so concurrent fetchers don't retry in lockstep.
                await asyncio.sleep(random.uniform(0.0, backoff))
                backoff = min(backoff * 2, self._backoff_cap)
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                raise BlockfrostNotFoundError(f"404 Not Found: {path}")
            if resp.status_code == 402:
                raise BlockfrostDailyLimitError(
                    "Blockfrost daily request limit reached (HTTP 402)."
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self._max_retries:
                    raise BlockfrostError(
                        f"{resp.status_code} after {attempt} retries: {path}"
                    )
                await asyncio.sleep(self._retry_delay(resp, backoff))
                backoff = min(backoff * 2, self._backoff_cap)
                continue
            raise BlockfrostError(f"{resp.status_code}: {resp.text[:200]}")

        raise BlockfrostError(f"Exhausted retries: {path}")  # pragma: no cover

    async def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        order: str = "asc",
        start_page: int = 1,
        max_items: int | None = None,
    ) -> AsyncIterator[tuple[int, list[dict[str, Any]]]]:
        """Yield `(page_number, page_items)` for a paginated list endpoint.

        Stops when an empty page is returned or `max_items` is reached.
        """
        count = self._settings.blockfrost_page_size
        page = start_page
        yielded = 0
        while True:
            page_params = {"count": count, "page": page, "order": order, **(params or {})}
            items = await self.get(path, params=page_params)
            if not items:
                return
            yield page, items
            yielded += len(items)
            if (max_items is not None and yielded >= max_items) or len(items) < count:
                return
            page += 1

    # --- Convenience endpoints -------------------------------------------------

    async def address_transactions(
        self,
        address: str,
        *,
        order: str = "asc",
        start_page: int = 1,
        max_items: int | None = None,
        from_block: str | None = None,
        to_block: str | None = None,
    ) -> AsyncIterator[tuple[int, list[dict[str, Any]]]]:
        params: dict[str, Any] = {}
        if from_block is not None:
            params["from"] = from_block
        if to_block is not None:
            params["to"] = to_block
        async for page, items in self.paginate(
            f"/addresses/{address}/transactions",
            params=params,
            order=order,
            start_page=start_page,
            max_items=max_items,
        ):
            yield page, items

    async def policy_assets(self, policy_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        async for _page, items in self.paginate(f"/assets/policy/{policy_id}"):
            yield items

    async def asset_transactions(self, asset: str) -> AsyncIterator[list[dict[str, Any]]]:
        async for _page, items in self.paginate(f"/assets/{asset}/transactions"):
            yield items

    async def tx(self, tx_hash: str) -> dict[str, Any]:
        return await self.get(f"/txs/{tx_hash}")

    async def tx_utxos(self, tx_hash: str) -> dict[str, Any]:
        return await self.get(f"/txs/{tx_hash}/utxos")

    async def address_info(self, address: str) -> dict[str, Any]:
        """`/addresses/{address}` — balances, script flag, stake address."""
        return await self.get(f"/addresses/{address}")

    async def script(self, script_hash: str) -> dict[str, Any]:
        """`/scripts/{hash}` — script type/version (plutusV1/V2/V3, timelock)."""
        return await self.get(f"/scripts/{script_hash}")

    async def asset(self, unit: str) -> dict[str, Any]:
        """`/assets/{unit}` — asset name and on-chain/registry metadata."""
        return await self.get(f"/assets/{unit}")
