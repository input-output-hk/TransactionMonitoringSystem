"""A block whose JSON is nested past the decoder's recursion limit.

Cardano native scripts are rendered by Ogmios as recursively nested JSON
(``{"clause": "all", "from": [...]}``) and the ledger does not bound how deep
that nesting goes, so any transaction can carry one deeper than ``json.loads``
survives. Before this was handled, such a block halted chain sync permanently:
the decode raised, the checkpoint never advanced, and the reconnect replayed
the identical frame every time. Preprod ran blind for 4.8 days on one 205 KB
block nested 10,774 deep.

Recall-first makes the trade one-sided: skipping one block loses that block,
while halting loses every block after it, so the skip wins as long as it is
recorded and alerted rather than silent. These tests pin both halves: the
quarantine happens, and it can never happen quietly or to an unidentified
block.

The second failure here was the retry path itself. ``ExponentialBackoff``
doubled an unbounded integer and multiplied it by a float, so once the attempt
counter passed 1024 the backoff raised ``OverflowError`` instead of waiting:
after ~20 h of any continuous failure, the mechanism meant to recover the
pipeline was the thing crashing it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.ingestion import ogmios_rpc, resilience
from app.ingestion.ogmios_client import BlockPersistError, OgmiosClient
from app.ingestion.ogmios_rpc import (
    FrameUndecodableError,
    max_nesting_depth,
    scan_block_point,
)

BLOCK_ID = "bac1" + "0" * 60
NEXT_TX_ID = "dead" + "1" * 60
# The depth that actually fails is bounded by the C stack, not by
# sys.getrecursionlimit(), so it varies by platform and by thread: measured
# between 3,000 and 5,000 on one machine and 10,774 in the preprod incident.
# This sits far above any of them so the test cannot pass for the wrong reason
# on a roomier stack. The shape is the observed one, nested `all` clauses from a
# native script, rather than a synthetic array.
POISON_NESTING = 20_000


def _nested_script(levels: int) -> str:
    """The native-script shape Ogmios emits, nested ``levels`` deep."""
    inner = '{"clause":"signature","from":"' + "ab" * 28 + '"}'
    for _ in range(levels):
        inner = '{"clause":"all","from":[' + inner + "]}"
    return inner


def _poison_frame(block_id: str = BLOCK_ID, slot: int = 133883340, levels: int = POISON_NESTING):
    """A nextBlock frame whose block carries a pathologically nested script."""
    return (
        '{"jsonrpc":"2.0","method":"nextBlock","result":{"direction":"forward",'
        f'"tip":{{"slot":999999,"id":"{"ff" * 32}"}},'
        f'"block":{{"type":"praos","era":"conway","id":"{block_id}",'
        f'"height":5183974,"slot":{slot},'
        f'"transactions":[{{"id":"{NEXT_TX_ID}","scripts":{{"x":'
        f"{_nested_script(levels)}}}}}]}}}}}}"
    )


class _StubWS:
    """Returns pre-canned frames in order, recording what was sent."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        return self._frames.pop(0)


# --- the decode boundary ---------------------------------------------------


@pytest.mark.asyncio
async def test_deeply_nested_frame_raises_typed_error_not_recursion_error():
    """The decoder's RecursionError must surface as FrameUndecodableError.

    The type is what lets chain sync tell "this frame will never decode" apart
    from a transport fault worth retrying; a bare RecursionError is caught by
    the generic handler and retried forever.
    """
    frame = _poison_frame()
    ws = _StubWS([frame])

    with pytest.raises(FrameUndecodableError) as excinfo:
        await ogmios_rpc.send_recv(ws, "nextBlock", None, request_id="1")

    assert excinfo.value.method == "nextBlock"
    assert excinfo.value.raw == frame
    assert isinstance(excinfo.value.cause, RecursionError)


@pytest.mark.asyncio
async def test_large_frames_take_the_same_path_through_the_executor():
    """The >threshold branch parses in a thread; it must translate too.

    The two branches are separate call sites, so a fix applied to only one
    leaves real (large) blocks crashing: the poison block is more likely to be
    large than small.
    """
    frame = _poison_frame()
    with patch.object(ogmios_rpc.settings, "OGMIOS_PARSE_EXECUTOR_THRESHOLD_BYTES", 1):
        with pytest.raises(FrameUndecodableError):
            await ogmios_rpc.send_recv(_StubWS([frame]), "nextBlock", None, request_id="1")


@pytest.mark.asyncio
async def test_translation_does_not_depend_on_reaching_a_real_stack_limit():
    """Pin the translation itself, independent of how deep this machine allows.

    The depth-driven tests above prove a real frame triggers it; this one keeps
    the contract covered on a platform whose stack swallows POISON_NESTING.
    """

    def _boom(_raw):
        raise RecursionError("maximum recursion depth exceeded")

    with patch("app.ingestion.ogmios_rpc.json.loads", _boom):
        with pytest.raises(FrameUndecodableError):
            await ogmios_rpc.send_recv(_StubWS(["{}"]), "nextBlock", None, request_id="1")


@pytest.mark.asyncio
async def test_ordinary_frames_are_unaffected():
    """The guard must not change the behaviour of every normal block."""
    payload = {"jsonrpc": "2.0", "result": {"direction": "forward"}, "id": "1"}
    got = await ogmios_rpc.send_recv(
        _StubWS([json.dumps(payload)]), "nextBlock", None, request_id="1"
    )
    assert got == payload


# --- recovering the block's identity without parsing -----------------------


def test_scan_recovers_the_block_point_from_an_unparseable_frame():
    block_id, slot = scan_block_point(_poison_frame(slot=133883340))
    assert block_id == BLOCK_ID
    assert slot == 133883340


def test_scan_reads_the_block_header_not_a_transaction_id():
    """A tx id is also 64 hex chars; picking one would checkpoint to a lie.

    The scan is confined to the segment before "transactions" precisely so the
    first id in the frame cannot be a transaction's.
    """
    block_id, _ = scan_block_point(_poison_frame())
    assert block_id == BLOCK_ID
    assert block_id != NEXT_TX_ID


def test_scan_reports_failure_rather_than_guessing():
    """No block segment means no point; the caller must fail closed."""
    assert scan_block_point('{"jsonrpc":"2.0","result":{"direction":"backward"}}') == (None, None)
    assert scan_block_point("") == (None, None)


def test_depth_ignores_brackets_inside_strings():
    """Otherwise an address or CBOR blob containing '[' inflates the depth."""
    assert max_nesting_depth('{"a":"[[[[[["}') == 1
    assert max_nesting_depth('{"a":"\\""}') == 1
    assert max_nesting_depth("[[[]]]") == 3
    assert max_nesting_depth(_nested_script(5)) == 11  # 2 brackets per level + the leaf


# --- the quarantine itself -------------------------------------------------


def _client():
    client = OgmiosClient()
    client.network = "preprod"
    return client


@pytest.mark.asyncio
async def test_quarantine_records_alerts_and_advances_past_the_block():
    """The three obligations of a skip: recorded, announced, checkpointed."""
    client = _client()
    events = []

    async def _collect(event):
        events.append(event)

    client.on_lifecycle_event = _collect
    frame = _poison_frame()
    error = FrameUndecodableError("nextBlock", frame, RecursionError("too deep"))

    record = AsyncMock()
    save = AsyncMock()
    with (
        patch("app.ingestion.ogmios_client.postgres.record_quarantined_block", record),
        patch("app.ingestion.ogmios_client.postgres.save_sync_point", save),
    ):
        await client._quarantine_undecodable_block(error)

    record.assert_awaited_once()
    assert record.await_args.args[:3] == ("preprod", BLOCK_ID, 133883340)
    assert record.await_args.kwargs["frame_bytes"] == len(frame)
    assert record.await_args.kwargs["nesting_depth"] > 1000

    save.assert_awaited_once_with("preprod", 133883340, BLOCK_ID)

    assert [e["eventType"] for e in events] == ["BLOCK_QUARANTINED"]
    assert events[0]["block"] == {"id": BLOCK_ID, "slot": 133883340}


@pytest.mark.asyncio
async def test_quarantine_is_durable_before_the_checkpoint_moves():
    """Order is load-bearing: a crash between the two must replay, not lose.

    If the checkpoint advanced first, a crash before the record was written
    would leave the block skipped with nothing anywhere saying so.
    """
    client = _client()
    order = []

    async def _record(*a, **k):
        order.append("record")

    async def _save(*a, **k):
        order.append("checkpoint")

    with (
        patch("app.ingestion.ogmios_client.postgres.record_quarantined_block", _record),
        patch("app.ingestion.ogmios_client.postgres.save_sync_point", _save),
    ):
        await client._quarantine_undecodable_block(
            FrameUndecodableError("nextBlock", _poison_frame(), RecursionError())
        )

    assert order == ["record", "checkpoint"]


@pytest.mark.asyncio
async def test_unidentifiable_block_is_never_checkpointed_past():
    """Without a recovered point, advancing would strand every block between.

    This is the case that must stay fail-closed: it falls back to the existing
    replay path rather than inventing a slot.
    """
    client = _client()
    save = AsyncMock()
    with (
        patch("app.ingestion.ogmios_client.postgres.record_quarantined_block", AsyncMock()),
        patch("app.ingestion.ogmios_client.postgres.save_sync_point", save),
        pytest.raises(BlockPersistError),
    ):
        await client._quarantine_undecodable_block(
            FrameUndecodableError("nextBlock", '{"no":"block here"}', RecursionError())
        )
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_quarantine_is_loud():
    """A silent skip is a recall hole nobody is told about."""
    client = _client()
    with (
        patch("app.ingestion.ogmios_client.postgres.record_quarantined_block", AsyncMock()),
        patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()),
        patch("app.ingestion.ogmios_client.logger") as log,
    ):
        await client._quarantine_undecodable_block(
            FrameUndecodableError("nextBlock", _poison_frame(), RecursionError())
        )
    log.critical.assert_called_once()


@pytest.mark.asyncio
async def test_sync_loop_keeps_going_after_a_quarantine():
    """The point of the whole change: the next block is still processed.

    Without this the fix is cosmetic. The loop must consume the undecodable
    frame, quarantine it, and go straight back to nextBlock rather than let the
    error escape to the reconnect handler, which would replay the same frame.
    """
    client = _client()
    client._running = True
    good = json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {"direction": "backward", "point": {"slot": 1, "id": "aa" * 32}},
        }
    )
    ws = _StubWS([_poison_frame(), good])

    handled = []

    async def _rollback(result):
        handled.append(result)
        client._running = False  # one good block is enough; stop the loop

    record = AsyncMock()
    with (
        patch(
            "app.ingestion.ogmios_client.postgres.get_sync_point",
            AsyncMock(return_value=None),
        ),
        patch("app.ingestion.ogmios_client.postgres.record_quarantined_block", record),
        patch("app.ingestion.ogmios_client.postgres.save_sync_point", AsyncMock()),
        patch.object(client, "_replay_pending_score_repurges", AsyncMock()),
        patch.object(client, "_fetch_slot_time_converter", AsyncMock()),
        patch.object(client, "_handle_roll_backward", _rollback),
        # The loop's own findIntersection handshake, before any nextBlock.
        patch.object(client, "_send_recv", wraps=client._send_recv) as send_recv,
    ):
        send_recv.side_effect = _intersect_then_stub(client, ws)
        await client._chain_sync_loop(ws)

    # Both halves, so the test cannot pass by the poison frame having decoded:
    # the block was quarantined AND the loop went on to the next frame.
    record.assert_awaited_once()
    assert record.await_args.args[1] == BLOCK_ID
    assert len(handled) == 1, "the block after the poison one was never processed"


def _intersect_then_stub(client, ws):
    """findIntersection answers from tip; nextBlock reads the stubbed frames."""

    async def _send_recv(_ws, method, params=None):
        if method == "findIntersection":
            return {"result": {"tip": {"slot": 1, "id": "aa" * 32}}}
        return await ogmios_rpc.send_recv(ws, method, params, request_id="1")

    return _send_recv


# --- the backoff that crashed instead of backing off -----------------------


def test_backoff_survives_an_attempt_count_that_overflows_a_float():
    """2**1024 exceeds the float range, so the old math raised OverflowError.

    ~20 h of continuous failure at the 60 s cap is enough to reach it, which a
    real outage did.
    """
    backoff = resilience.ExponentialBackoff(base_delay=1.0, max_delay=60.0)
    backoff.attempt = 5000
    delay = min(backoff.base_delay * (2 ** min(backoff.attempt, backoff._max_exponent)), 60.0)
    assert delay == 60.0


@pytest.mark.asyncio
async def test_backoff_still_waits_the_capped_delay_after_many_failures():
    """Bounding the exponent must not stop it backing off."""
    backoff = resilience.ExponentialBackoff(base_delay=1.0, max_delay=60.0, jitter_factor=0.0)
    backoff.attempt = 100_000
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    with patch("app.ingestion.resilience.asyncio.sleep", _sleep):
        await backoff.wait()

    assert slept == [60.0]
    assert backoff.attempt == 100_001


def test_backoff_still_ramps_from_the_bottom():
    """The cap must only bite at the top; early attempts still double."""
    backoff = resilience.ExponentialBackoff(base_delay=1.0, max_delay=60.0)
    delays = []
    for attempt in range(8):
        backoff.attempt = attempt
        delays.append(
            min(backoff.base_delay * (2 ** min(attempt, backoff._max_exponent)), backoff.max_delay)
        )
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_exponent_ceiling_handles_degenerate_delays():
    """A misconfigured pair must not produce a negative or absurd exponent."""
    assert resilience._exponent_ceiling(0.0, 60.0) == 0
    assert resilience._exponent_ceiling(60.0, 60.0) == 0
    assert resilience._exponent_ceiling(100.0, 60.0) == 0
    assert resilience._exponent_ceiling(1.0, 60.0) == 6
