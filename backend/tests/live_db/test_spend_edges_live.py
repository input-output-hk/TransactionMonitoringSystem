"""Live-ClickHouse tests for spend_edges: the backfill makes the writer's edges.

The block writer builds edges in Python from the transactions it inserts; the
one-time backfill builds them in SQL from the rows already stored. A history
filled differently from what live ingestion writes would score old and new
transactions against different graphs, so the two must agree row for row,
replays and rollbacks included. Requires TMS_LIVE_DB_TESTS=1 (see conftest).
"""

import uuid
from datetime import UTC, datetime

from app.db import spend_edges
from app.models.transaction import NormalizedTransaction, TransactionInput, TransactionOutput

from .conftest import LIVE_NETWORK

_SLOT = 3_000_000


def _network() -> str:
    return f"{LIVE_NETWORK}-edges-{uuid.uuid4().hex[:8]}"


def _tx(network, inputs, outputs, slot=_SLOT) -> NormalizedTransaction:
    return NormalizedTransaction(
        tx_hash=uuid.uuid4().hex * 2,
        network=network,
        slot=slot,
        block_height=slot,
        block_hash="ab" * 32,
        block_index=0,
        timestamp=datetime.now(UTC),
        fee=200_000,
        inputs=inputs,
        outputs=outputs,
        input_count=len(inputs),
        output_count=len(outputs),
        addresses=[i.address for i in inputs if i.address] + [o.address for o in outputs],
    )


def _in(address, **flags) -> TransactionInput:
    return TransactionInput(tx_hash="cd" * 32, index=0, address=address, amount=1, **flags)


def _out(address, amount, **flags) -> TransactionOutput:
    return TransactionOutput(address=address, amount=amount, **flags)


def _edges(ch, network) -> set[tuple]:
    return set(
        ch._get_client().execute(
            "SELECT address, slot, tx_hash, to_address, paid FROM spend_edges FINAL "
            "WHERE network = %(network)s",
            {"network": network},
        )
    )


def _plant(ch, network) -> list[NormalizedTransaction]:
    run = uuid.uuid4().hex[:8]
    a, b, c = (f"addr_test1qq{leg}_{run}" for leg in "abc")
    txs = [
        # Two spenders, two payees.
        _tx(network, [_in(a), _in(b)], [_out(b, 5), _out(c, 7)]),
        # Equal outputs to one address, a collateral return, and inputs that
        # move no value.
        _tx(
            network,
            [_in(c), _in(a, is_collateral=True), _in(b, is_reference=True), _in("")],
            [_out(a, 4), _out(a, 4), _out(c, 9, is_collateral=True)],
        ),
        # A phase-2 failure: its regular inputs never spent.
        _tx(network, [_in(a, is_unspent_attempt=True)], [_out(b, 3)]),
    ]
    ch.insert_transactions_batch(txs)
    # A replayed block re-inserts the same rows.
    ch.insert_transactions_batch(txs[:1])
    # A slotless mempool copy of a transaction confirmed later.
    ch.insert_transactions_batch([txs[0].model_copy(update={"slot": None})])
    return txs


def _refill(ch, network, txs) -> None:
    ch._get_client().execute(
        "DELETE FROM spend_edges WHERE network = %(network)s", {"network": network}
    )
    hex_digits = spend_edges._HEX_DIGITS_PER_CHUNK
    for chunk in sorted({int(tx.tx_hash[:hex_digits], 16) for tx in txs}):
        spend_edges.backfill_chunk(network, chunk)


class TestBackfill:
    def test_the_backfill_makes_exactly_the_writer_s_edges(self, ch):
        network = _network()
        txs = _plant(ch, network)
        written = _edges(ch, network)
        assert written, "the writer made no edges"

        _refill(ch, network, txs)

        assert _edges(ch, network) == written

    def test_a_purged_transaction_gets_no_edges_back(self, ch):
        """After a rollback purge its source rows are gone too, so a backfill
        running after it cannot bring an off-chain spend back."""
        network = _network()
        txs = _plant(ch, network)
        ch.delete_rolled_back_txs(network, _SLOT - 1)
        assert _edges(ch, network) == set()

        _refill(ch, network, txs)

        assert _edges(ch, network) == set()

    def test_a_chunk_reads_only_its_own_range(self, ch):
        network = _network()
        txs = _plant(ch, network)
        hex_digits = spend_edges._HEX_DIGITS_PER_CHUNK
        chunk = int(txs[0].tx_hash[:hex_digits], 16)
        ch._get_client().execute(
            "DELETE FROM spend_edges WHERE network = %(network)s", {"network": network}
        )

        spend_edges.backfill_chunk(network, chunk)

        assert {edge[2][:hex_digits] for edge in _edges(ch, network)} == {txs[0].tx_hash[:2]}
