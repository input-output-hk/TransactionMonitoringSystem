"""Slot-to-UTC conversion and its chain-sync wiring (Ticket F).

transactions.timestamp is chain time for the baselines' 90/180-day
windows; before the converter it was stamped with ingestion wall clock,
so a catch-up replay collapsed months of history into "now". These pin
the era-summary math (mainnet-like Byron 20s slots then Shelley 1s
slots) and the wall-clock fallback that keeps ingestion alive when the
node cannot answer the queries.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.ingestion.chain_time import SlotTimeConverter
from app.ingestion.ogmios_client import OgmiosClient

SYSTEM_START = "2017-09-23T21:44:51Z"
SYSTEM_START_DT = datetime.fromisoformat(SYSTEM_START)

# Mainnet-shaped fixtures: Byron ran 20-second slots for 208 epochs of
# 21600 slots, so Shelley starts at slot 4_492_800, offset 89_856_000
# seconds after systemStart, with 1-second slots from there on.
BYRON_SLOT_SECONDS = 20
SHELLEY_START_SLOT = 4_492_800
SHELLEY_START_SECONDS = SHELLEY_START_SLOT * BYRON_SLOT_SECONDS

ERA_SUMMARIES = [
    {
        "start": {"time": {"seconds": 0}, "slot": 0, "epoch": 0},
        "end": {
            "time": {"seconds": SHELLEY_START_SECONDS},
            "slot": SHELLEY_START_SLOT,
            "epoch": 208,
        },
        "parameters": {
            "epochLength": 21_600,
            "slotLength": {"milliseconds": 20_000},
            "safeZone": 4_320,
        },
    },
    {
        "start": {
            "time": {"seconds": SHELLEY_START_SECONDS},
            "slot": SHELLEY_START_SLOT,
            "epoch": 208,
        },
        "end": {
            "time": {"seconds": SHELLEY_START_SECONDS + 432_000},
            "slot": SHELLEY_START_SLOT + 432_000,
            "epoch": 209,
        },
        "parameters": {
            "epochLength": 432_000,
            "slotLength": {"milliseconds": 1_000},
            "safeZone": 129_600,
        },
    },
]


def _run(coro):
    return asyncio.run(coro)


def _converter():
    conv = SlotTimeConverter.from_ogmios(SYSTEM_START, ERA_SUMMARIES)
    assert conv is not None
    return conv


class TestSlotToUtc:
    def test_genesis_slot_is_system_start(self):
        assert _converter().slot_to_utc(0) == SYSTEM_START_DT

    def test_byron_slot_uses_20s_slots(self):
        expected = SYSTEM_START_DT + timedelta(seconds=100 * BYRON_SLOT_SECONDS)
        assert _converter().slot_to_utc(100) == expected

    def test_era_boundary_slot_starts_shelley(self):
        expected = SYSTEM_START_DT + timedelta(seconds=SHELLEY_START_SECONDS)
        assert _converter().slot_to_utc(SHELLEY_START_SLOT) == expected

    def test_shelley_slot_uses_1s_slots(self):
        expected = SYSTEM_START_DT + timedelta(seconds=SHELLEY_START_SECONDS + 7)
        assert _converter().slot_to_utc(SHELLEY_START_SLOT + 7) == expected

    def test_slot_beyond_forecast_horizon_extrapolates(self):
        # A block existing past the last summary's `end` is still in the
        # current era; the converter must extrapolate, not go dark.
        far = SHELLEY_START_SLOT + 100_000_000
        expected = SYSTEM_START_DT + timedelta(
            seconds=SHELLEY_START_SECONDS + 100_000_000
        )
        assert _converter().slot_to_utc(far) == expected

    def test_slot_before_first_known_era_is_none(self):
        conv = SlotTimeConverter.from_ogmios(
            SYSTEM_START,
            [
                {
                    "start": {"time": {"seconds": 2_000}, "slot": 100, "epoch": 1},
                    "parameters": {"slotLength": {"milliseconds": 1_000}},
                }
            ],
        )
        assert conv.slot_to_utc(99) is None
        assert conv.slot_to_utc(100) is not None

    @pytest.mark.parametrize("bad_slot", [None, -1])
    def test_unusable_slots_are_none(self, bad_slot):
        assert _converter().slot_to_utc(bad_slot) is None

    def test_result_is_utc_aware(self):
        ts = _converter().slot_to_utc(SHELLEY_START_SLOT)
        assert ts.tzinfo is not None
        assert ts.utcoffset().total_seconds() == 0


class TestFromOgmios:
    def test_naive_start_time_assumed_utc(self):
        conv = SlotTimeConverter.from_ogmios(
            "2017-09-23T21:44:51", ERA_SUMMARIES
        )
        assert conv.slot_to_utc(0) == SYSTEM_START_DT

    def test_bare_seconds_slot_length_accepted(self):
        conv = SlotTimeConverter.from_ogmios(
            SYSTEM_START,
            [
                {
                    "start": {"time": {"seconds": 0}, "slot": 0, "epoch": 0},
                    "parameters": {"slotLength": 2},
                }
            ],
        )
        assert conv.slot_to_utc(10) == SYSTEM_START_DT + timedelta(seconds=20)

    @pytest.mark.parametrize(
        "start_time,summaries",
        [
            (None, ERA_SUMMARIES),
            ({"weird": 1}, ERA_SUMMARIES),
            ("not-a-date", ERA_SUMMARIES),
            (SYSTEM_START, None),
            (SYSTEM_START, []),
            (SYSTEM_START, [{"start": {}}]),
            (SYSTEM_START, [{"start": {"time": {"seconds": 0}, "slot": 0},
                             "parameters": {"slotLength": {"milliseconds": 0}}}]),
            (SYSTEM_START, "origin"),
        ],
        ids=[
            "no-start", "dict-start", "garbage-start", "no-summaries",
            "empty-summaries", "missing-keys", "zero-slot-length",
            "string-summaries",
        ],
    )
    def test_unusable_inputs_yield_none(self, start_time, summaries):
        assert SlotTimeConverter.from_ogmios(start_time, summaries) is None


def _block(slot):
    return {
        "block": {
            "id": "ab" * 32,
            "slot": slot,
            "height": 7,
            "transactions": [
                {
                    "id": "00" * 32,
                    "spends": "inputs",
                    "fee": {"ada": {"lovelace": 200_000}},
                    "inputs": [{"transaction": {"id": "11" * 32}, "index": 0}],
                    "outputs": [
                        {"address": "addr_test1qq", "value": {"ada": {"lovelace": 1}}}
                    ],
                }
            ],
        },
        "tip": {"slot": slot + 10},
    }


def _persistence_patches(insert=None):
    return [
        patch(
            "app.ingestion.ogmios_client.clickhouse.insert_transactions_batch_async",
            insert or AsyncMock(),
        ),
        patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()),
        patch(
            "app.ingestion.ogmios_client.postgres.batch_upsert_lifecycle_confirmed",
            AsyncMock(),
        ),
        patch(
            "app.ingestion.ogmios_client.clickhouse.get_outputs_for_refs_async",
            AsyncMock(return_value={}),
        ),
    ]


class TestChainSyncWiring:
    def _roll_forward(self, client, slot, monkeypatch):
        from contextlib import ExitStack

        monkeypatch.setattr(settings, "RAW_STORE_ENABLED", False)
        inserted = {}

        async def capture_insert(txs):
            inserted["txs"] = txs

        with ExitStack() as stack:
            for p in _persistence_patches(insert=capture_insert):
                stack.enter_context(p)
            _run(client._handle_roll_forward(_block(slot)))
        return inserted["txs"]

    def test_block_timestamp_is_chain_time(self, monkeypatch):
        client = OgmiosClient()
        client._slot_time = _converter()
        txs = self._roll_forward(client, SHELLEY_START_SLOT + 30, monkeypatch)
        assert txs[0].timestamp == SYSTEM_START_DT + timedelta(
            seconds=SHELLEY_START_SECONDS + 30
        )

    def test_block_timestamp_falls_back_to_wall_clock(self, monkeypatch):
        client = OgmiosClient()
        assert client._slot_time is None
        before = datetime.now(timezone.utc)
        txs = self._roll_forward(client, SHELLEY_START_SLOT + 30, monkeypatch)
        assert txs[0].timestamp >= before

    def test_fetch_builds_converter_from_queries(self):
        client = OgmiosClient()
        responses = {
            "queryNetwork/startTime": {"result": SYSTEM_START},
            "queryLedgerState/eraSummaries": {"result": ERA_SUMMARIES},
        }

        async def send_recv(ws, method, params=None):
            return responses[method]

        with patch.object(client, "_send_recv", send_recv):
            _run(client._fetch_slot_time_converter(object()))
        assert client._slot_time is not None
        assert client._slot_time.slot_to_utc(0) == SYSTEM_START_DT

    def test_fetch_failure_leaves_wall_clock_fallback(self):
        client = OgmiosClient()
        with patch.object(
            client, "_send_recv", AsyncMock(side_effect=RuntimeError("closed"))
        ):
            _run(client._fetch_slot_time_converter(object()))  # must not raise
        assert client._slot_time is None

    def test_error_response_leaves_wall_clock_fallback(self):
        client = OgmiosClient()
        with patch.object(
            client,
            "_send_recv",
            AsyncMock(return_value={"error": {"code": -32601, "message": "nope"}}),
        ):
            _run(client._fetch_slot_time_converter(object()))
        assert client._slot_time is None

    def test_chain_sync_loop_fetches_converter_before_blocks(self):
        client = OgmiosClient()
        order = []

        async def fetch(ws):
            order.append("fetch")

        async def send_recv(ws, method, params=None):
            order.append(method)
            return {"result": {}}

        client._running = False  # skip the nextBlock loop body
        client._replay_pending_score_repurges = AsyncMock()
        with patch.object(client, "_fetch_slot_time_converter", fetch), \
             patch.object(client, "_send_recv", send_recv), \
             patch("app.ingestion.ogmios_client.postgres.get_sync_point",
                   AsyncMock(return_value=None)):
            _run(client._chain_sync_loop(ws=object()))
        assert order[0] == "fetch"
