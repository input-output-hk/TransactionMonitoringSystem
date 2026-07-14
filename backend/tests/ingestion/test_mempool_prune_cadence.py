"""Deterministic cadence for the mempool orphan-TTL/cap prune sweep.

The old trigger (len(_seen_mempool_txs) % N == 0) depended on the dedup
set's length, which is not monotonic: confirms discard entries and
rollbacks/reconnects clear the set, so the modulo could be skipped forever
and the TTL sweep would never evict stale pending entries or orphaned
input-cache entries in a quiet mempool. The sweep must fire once per N
PROCESSED txs, independent of set-size churn.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.ingestion.mempool_monitor import MempoolMonitor

# Small sweep interval so the tests exercise several full cycles cheaply.
PRUNE_EVERY = 3


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "MEMPOOL_PRUNE_EVERY_N_TXS", PRUNE_EVERY)
    return MempoolMonitor(
        network="preprod",
        emit=AsyncMock(),
        query_utxo=AsyncMock(return_value=[]),
        connect_ws=AsyncMock(),
        send_recv=AsyncMock(),
    )


def _tx(i: int) -> tuple:
    """A mempool tx with a unique input ref (no collisions, no DB calls)."""
    tx_id = f"{i:02x}" * 32
    tx_data = {
        "inputs": [{"transaction": {"id": f"{i + 0x40:02x}" * 32}, "index": 0}],
    }
    return tx_id, tx_data


def _process(client, count, churn):
    """Run ``count`` txs through _record_mempool_collisions, applying
    ``churn(i)`` to the dedup set between calls to simulate confirms,
    rollbacks, and reconnects mutating its size arbitrarily."""

    async def scenario():
        now = datetime.now(UTC)
        for i in range(count):
            tx_id, tx_data = _tx(i)
            await client._record_mempool_collisions(tx_id, tx_data, now)
            churn(i)

    asyncio.run(scenario())


class TestPruneCadence:
    def _spy_sweeps(self, client):
        sweeps = []
        original = client._pending.stale_ids

        def spy(cutoff):
            sweeps.append(cutoff)
            return original(cutoff)

        client._pending.stale_ids = spy
        return sweeps

    def test_sweep_fires_every_n_txs_with_constant_set_size(self, client):
        # The stall case for the old modulo trigger: the dedup set stays at
        # a size that is never a multiple of N (confirms remove one entry
        # for each one added), so len % N == 0 would never be true.
        sweeps = self._spy_sweeps(client)
        stuck_set = {"w" * 64, "x" * 64, "y" * 64, "z" * 64}
        client._seen_mempool_txs = set(stuck_set)
        _process(
            client,
            PRUNE_EVERY * 2,
            churn=lambda i: client._seen_mempool_txs.update(stuck_set),
        )
        assert len(sweeps) == 2  # fired on the 3rd and 6th processed tx

    def test_sweep_fires_when_set_is_cleared_every_call(self, client):
        # Rollbacks/reconnects clear the set; cadence must be unaffected.
        sweeps = self._spy_sweeps(client)
        _process(
            client,
            PRUNE_EVERY * 2,
            churn=lambda i: client._seen_mempool_txs.clear(),
        )
        assert len(sweeps) == 2

    def test_counter_resets_after_each_sweep(self, client):
        sweeps = self._spy_sweeps(client)
        _process(client, PRUNE_EVERY * 3 + 1, churn=lambda i: None)
        assert len(sweeps) == 3
        assert client._mempool_txs_since_prune == 1  # remainder carried over

    def test_sweep_actually_evicts_stale_state(self, client, monkeypatch):
        # End-to-end effect of a fired sweep: a pending entry past the TTL
        # is untracked and its orphaned input-cache entry evicted.
        monkeypatch.setattr(settings, "MEMPOOL_PENDING_TTL_SECONDS", 60)
        stale_at = datetime.now(UTC) - timedelta(seconds=3600)
        stale_id = "ee" * 32
        client._pending.track(stale_id, ({("dd" * 32, 0)}, stale_at, 0, "", 0))
        client._pending_input_cache[stale_id] = ({}, stale_at)
        orphan_id = "cc" * 32  # cache entry whose tx never reached the index
        client._pending_input_cache[orphan_id] = ({}, stale_at)

        _process(client, PRUNE_EVERY, churn=lambda i: None)

        assert client._pending.get(stale_id) is None
        assert stale_id not in client._pending_input_cache
        assert orphan_id not in client._pending_input_cache
