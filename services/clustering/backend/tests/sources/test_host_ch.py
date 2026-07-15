"""Unit tests for ``HostChainSource.metadata`` — the onboarding entry point.

These pin the client-facing behaviour of the zero-row path: the error must be
marked ``client_safe`` (so ``_safe_error`` surfaces the real reason rather than
the generic "not found on-chain"), and it must distinguish "before this
instance's data begins" from "the instance has indexed nothing yet"."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from app.config import Settings
from app.sources.base import SourceNotFound
from app.sources.host_ch.source import _EPOCH_YEAR, HostChainSource

# A mainnet script address (Djed-shaped) used purely as an onboarding target.
_ADDR = "addr1wxy49hzx86ch868hr3uz98lqw8p7ef55j6x8ras7udy3a0gm8cdla"


class _FakeResult:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.result_rows = rows


class _FakeClient:
    """Dispatches the two queries ``metadata`` issues on the zero-row path by
    matching the aggregate in the SQL, so a test controls both independently."""

    def __init__(self, *, count: int, floor: datetime | None) -> None:
        self._count = count
        self._floor = floor

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        if "count()" in sql:
            return _FakeResult([[self._count]])
        if "min(timestamp)" in sql:
            return _FakeResult([[self._floor]])
        raise AssertionError(f"unexpected query: {sql}")


def _source(*, count: int, floor: datetime | None, network: str = "mainnet") -> HostChainSource:
    # Constructor kwargs outrank any ambient .env, keeping the test deterministic.
    src = HostChainSource(Settings(cardano_network=network, host_clickhouse_db="tms_analytics"))
    src._client = _FakeClient(count=count, floor=floor)
    return src


async def test_zero_rows_reports_instance_data_floor() -> None:
    """An address the instance has not synced back to fails with a client-safe
    message naming how far back the network is indexed, not "not found"."""
    src = _source(count=0, floor=datetime(2026, 7, 15), network="mainnet")
    with pytest.raises(SourceNotFound) as ei:
        await src.metadata(_ADDR, "address")
    exc = ei.value
    assert exc.client_safe is True
    msg = str(exc)
    assert "mainnet" in msg
    assert "2026-07-15" in msg
    # It must not claim the address is absent from the chain.
    assert "not found on-chain" not in msg


async def test_zero_rows_empty_network_says_nothing_indexed() -> None:
    """min(timestamp) over an empty partition is the DateTime zero value; that
    is reported as 'nothing indexed yet', never as a bogus 1970 date."""
    src = _source(count=0, floor=datetime(_EPOCH_YEAR, 1, 1), network="preprod")
    with pytest.raises(SourceNotFound) as ei:
        await src.metadata(_ADDR, "address")
    msg = str(ei.value)
    assert ei.value.client_safe is True
    assert "indexed yet" in msg
    assert str(_EPOCH_YEAR) not in msg


async def test_non_address_target_is_client_safe() -> None:
    src = _source(count=0, floor=None)
    with pytest.raises(SourceNotFound) as ei:
        await src.metadata("pool1xyz", "policy")
    assert ei.value.client_safe is True
    assert "address" in str(ei.value)


async def test_present_address_returns_metadata() -> None:
    src = _source(count=42, floor=None)
    meta = await src.metadata(_ADDR, "address")
    assert meta["exists"] is True
    # The target is a script payment address (header type 7), so is_script holds.
    assert meta["is_script"] is True
