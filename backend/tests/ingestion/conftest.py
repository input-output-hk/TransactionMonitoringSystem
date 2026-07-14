"""Shared scaffolding for the ingestion test suite.

Only the genuinely common pieces live here, mirroring the scorers
conftest convention: the asyncio runner, the representative Ogmios
block envelope, and the persistence patch-set for _handle_roll_forward.
Intent-specific factories (e.g. the enrichment suite's parser-driven tx
builder) stay in their own modules so each test still shows which
inputs it consumes.
"""

import asyncio
from unittest.mock import AsyncMock, patch


def run_async(coro):
    return asyncio.run(coro)


def make_block(slot=100, txs=1):
    """A representative Ogmios ``nextBlock`` rollForward result."""
    return {
        "block": {
            "id": "ab" * 32,
            "slot": slot,
            "height": 7,
            "transactions": [
                {
                    "id": f"{i:02d}" * 32,
                    "spends": "inputs",
                    "fee": {"ada": {"lovelace": 200_000}},
                    "inputs": [{"transaction": {"id": "11" * 32}, "index": 0}],
                    "outputs": [
                        {"address": "addr_test1qq", "value": {"ada": {"lovelace": 1}}}
                    ],
                }
                for i in range(txs)
            ],
        },
        "tip": {"slot": slot + 10},
    }


def persistence_patches(insert=None, save_sync=None, raw_write=None):
    """Patches for every persistence side effect of _handle_roll_forward.

    A new side effect added to the roll-forward path must be added HERE,
    not per test file: the review found two divergent copies of this
    list, which is exactly how a new dependency starts hitting real DB
    clients in half the suite.
    """
    patches = [
        patch(
            "app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async",
            insert or AsyncMock(),
        ),
        patch(
            "app.ingestion.ogmios_client.postgres.save_sync_point",
            save_sync or AsyncMock(),
        ),
        patch(
            "app.ingestion.ogmios_client.postgres.batch_upsert_lifecycle_confirmed",
            AsyncMock(),
        ),
        patch(
            "app.ingestion.ogmios_client.clickhouse.get_outputs_for_refs_async",
            AsyncMock(return_value={}),
        ),
    ]
    if raw_write is not None:
        patches.append(
            patch("app.ingestion.ogmios_client.raw_store.write_confirmed", raw_write)
        )
    return patches
