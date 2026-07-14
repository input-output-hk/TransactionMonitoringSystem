"""Failed attack attempts must stay visible to contention recording.

Recall claim of the failed-attack-visibility fix: a double-satisfaction
ATTEMPT that fails phase-2 validation (script_valid=false) still carries
the inputs it TRIED to spend (flagged is_unspent_attempt), and those
attempted inputs must keep producing displacement/contention signals.
Built against the Invariant-0 cardano-ctf 01-sell-nft shape: the attacker
races the legitimate buyer for the same script UTxO, loses, and confirms
on-chain as a failed tx that consumed only its collateral.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.ingestion.mempool_monitor import MempoolMonitor
from app.models.transaction import NormalizedTransaction, TransactionInput
from tests.ingestion.conftest import run_async as _run

ATTEMPT_HASH = "a7" * 32  # the failed double-sat attempt
WINNER_HASH = "b8" * 32  # the legitimate tx that won the contested UTxO
CONTESTED_REF = ("c9" * 32, 1)  # the script UTxO both txs raced for
COLLATERAL_REF = ("d0" * 32, 0)  # the attempt's collateral input


@pytest.fixture
def client():
    return MempoolMonitor(
        network="preprod",
        emit=AsyncMock(),
        query_utxo=AsyncMock(return_value=[]),
        connect_ws=AsyncMock(),
        send_recv=AsyncMock(),
    )


def _confirmed_tx(tx_hash, *, script_valid, inputs, fee=200_000):
    return NormalizedTransaction(
        tx_hash=tx_hash,
        network="preprod",
        timestamp=datetime.now(timezone.utc),
        fee=fee,
        script_valid=script_valid,
        inputs=inputs,
        outputs=[],
        raw_data={},
    )


def _attempted_input(ref, address="addr_test1qattacker"):
    # Regular input of a phase-2-failed tx: referenced but NOT consumed by
    # the ledger; persisted flagged so the attempt target stays visible.
    return TransactionInput(
        tx_hash=ref[0],
        index=ref[1],
        address=address,
        amount=0,
        is_reference=False,
        is_collateral=False,
        is_unspent_attempt=True,
    )


def _collateral_input(ref, address="addr_test1qattacker"):
    return TransactionInput(
        tx_hash=ref[0],
        index=ref[1],
        address=address,
        amount=5_000_000,
        is_reference=False,
        is_collateral=True,
    )


class TestConsumedRefsForFailedAttempt:
    def test_failed_attempt_consumes_collateral_not_attempted_inputs(self):
        # Ledger semantics: phase-2 failure spends the collateral; the
        # attempted regular inputs stay live (the victim can still be hit).
        tx = _confirmed_tx(
            ATTEMPT_HASH,
            script_valid=False,
            inputs=[_attempted_input(CONTESTED_REF), _collateral_input(COLLATERAL_REF)],
        )
        assert MempoolMonitor._consumed_refs(tx) == {COLLATERAL_REF}

    def test_attempted_inputs_remain_visible_on_the_parsed_tx(self):
        # The pre-fix parser DROPPED a failed tx's regular inputs, blinding
        # every contention reader to what the attack targeted.
        tx = _confirmed_tx(
            ATTEMPT_HASH,
            script_valid=False,
            inputs=[_attempted_input(CONTESTED_REF), _collateral_input(COLLATERAL_REF)],
        )
        attempted = [(i.tx_hash, i.index) for i in tx.inputs if i.is_unspent_attempt]
        assert attempted == [CONTESTED_REF]


class TestFailedAttemptDisplacementSignal:
    def test_pending_attempts_attempted_inputs_produce_displacement(self, client):
        """End-to-end contention path: the failed double-sat attempt is
        observed in the mempool (its attempted input refs enter the pending
        index), then the legitimate tx confirms consuming the contested
        ref. The attempt's attempted inputs must produce the displacement
        record naming it as the displaced party."""
        seen_at = datetime.now(timezone.utc)
        attempt_mempool_data = {
            "inputs": [
                {
                    "transaction": {"id": CONTESTED_REF[0]},
                    "index": CONTESTED_REF[1],
                    "address": "addr_test1qattacker",
                },
            ],
        }
        winner = _confirmed_tx(
            WINNER_HASH,
            script_valid=True,
            inputs=[
                TransactionInput(
                    tx_hash=CONTESTED_REF[0],
                    index=CONTESTED_REF[1],
                    address="addr_test1qbuyer",
                    amount=10_000_000,
                    is_reference=False,
                    is_collateral=False,
                )
            ],
        )
        insert_collision = AsyncMock()
        outcome = AsyncMock()

        async def scenario():
            # Mempool side: the attempt registers its attempted refs.
            await client._record_mempool_collisions(
                ATTEMPT_HASH,
                attempt_mempool_data,
                seen_at,
            )
            # Chain side: the winner confirms, consuming the contested ref.
            await client.record_displacements(
                [winner],
                datetime.now(timezone.utc),
            )

        with (
            patch(
                "app.ingestion.mempool_monitor.postgres.insert_mempool_collision", insert_collision
            ),
            patch("app.ingestion.mempool_monitor.postgres.update_collision_outcome", outcome),
        ):
            _run(scenario())

        insert_collision.assert_awaited_once()
        kwargs = insert_collision.await_args.kwargs
        assert kwargs["tx_a"] == ATTEMPT_HASH  # the displaced attempt
        assert kwargs["tx_b"] == WINNER_HASH  # the confirmed winner
        assert kwargs["shared_inputs"] == [list(CONTESTED_REF)]
        outcome.assert_awaited_once_with(WINNER_HASH, client.network)

    def test_confirmed_failed_attempt_does_not_fake_displacement(self, client):
        """The inverse guard: when the FAILED attempt itself confirms, its
        attempted inputs were never consumed, so a pending tx wanting the
        same ref was NOT displaced (it can still confirm) and must not get
        a false displacement record. The attempt's collateral, which the
        ledger DID spend, is what produces a signal."""
        seen_at = datetime.now(timezone.utc)
        # A pending tx wanting the contested ref the attempt failed to take.
        client._pending.track(
            "e1" * 32,
            ({CONTESTED_REF}, seen_at, 180_000, "addr_test1qv", 0),
        )
        # A pending tx wanting the attempt's collateral UTxO (consumed).
        client._pending.track(
            "f2" * 32,
            ({COLLATERAL_REF}, seen_at, 180_000, "addr_test1qw", 0),
        )
        failed_attempt = _confirmed_tx(
            ATTEMPT_HASH,
            script_valid=False,
            inputs=[_attempted_input(CONTESTED_REF), _collateral_input(COLLATERAL_REF)],
        )
        insert_collision = AsyncMock()
        with (
            patch(
                "app.ingestion.mempool_monitor.postgres.insert_mempool_collision", insert_collision
            ),
            patch("app.ingestion.mempool_monitor.postgres.update_collision_outcome", AsyncMock()),
        ):
            _run(
                client.record_displacements(
                    [failed_attempt],
                    datetime.now(timezone.utc),
                )
            )

        insert_collision.assert_awaited_once()
        kwargs = insert_collision.await_args.kwargs
        assert kwargs["tx_a"] == "f2" * 32  # collateral contender, not "e1"
        assert kwargs["shared_inputs"] == [list(COLLATERAL_REF)]
