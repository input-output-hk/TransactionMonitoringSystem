"""Live-ClickHouse tests for spend_edges: the backfill makes the writer's edges.

The block writer builds edges in Python from the transactions it inserts; the
one-time backfill builds them in SQL from the rows already stored. A history
filled differently from what live ingestion writes would score old and new
transactions against different graphs, so the two must agree row for row,
replays and rollbacks included. The coverage check run at every start must
find exactly the chunks whose stored spends lost their edges, and refilling a
chunk that still has them must change no read. Requires TMS_LIVE_DB_TESTS=1
(see conftest).
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.analysis import dex, graph
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
    # A slotless mempool copy of a transaction confirmed later, so stamped
    # earlier: transactions keeps one version per tx_hash, and a copy stamped
    # after the confirmation would replace it on the next merge, taking its
    # slot (and with it the backfill's edges) away whenever a merge ran first.
    seen_in_mempool = datetime.now(UTC) - timedelta(minutes=1)
    ch.insert_transactions_batch(
        [txs[0].model_copy(update={"slot": None, "ingestion_timestamp": seen_in_mempool})]
    )
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


def _in_chunk(tx: NormalizedTransaction, chunk: int) -> NormalizedTransaction:
    """The transaction with its hash moved into the given backfill chunk."""
    prefix, _ = spend_edges._chunk_bounds(chunk)
    return tx.model_copy(update={"tx_hash": prefix + tx.tx_hash[len(prefix) :]})


def _drop_chunk_edges(ch, network, chunk) -> None:
    """What a restore or a lost part leaves: one chunk of the history without edges."""
    prefix, _ = spend_edges._chunk_bounds(chunk)
    ch._get_client().execute(
        "DELETE FROM spend_edges WHERE network = %(network)s AND startsWith(tx_hash, %(prefix)s)",
        {"network": network, "prefix": prefix},
    )


def _addresses(run) -> tuple[str, str, str]:
    a, b, c = (f"addr_test1qq{leg}_{run}" for leg in "abc")
    return a, b, c


# Two chunks far apart, so the 16-chunk passes check them in different queries.
_CHUNK_A, _CHUNK_B = 0x13, 0xA7


class TestMissingChunks:
    """The coverage check finds exactly the chunks whose stored spends lost
    their edges, and nothing the fill itself would leave without edges."""

    def test_only_the_chunk_whose_edges_are_gone_is_reported(self, ch):
        network = _network()
        a, b, c = _addresses(uuid.uuid4().hex[:8])
        txs = [
            _in_chunk(_tx(network, [_in(a)], [_out(b, 5)]), _CHUNK_A),
            _in_chunk(_tx(network, [_in(b)], [_out(c, 7)]), _CHUNK_B),
        ]
        ch.insert_transactions_batch(txs)
        before = datetime.now(UTC) + timedelta(minutes=1)
        assert spend_edges.missing_chunks(network, before) == [], "the writer made them"

        _drop_chunk_edges(ch, network, _CHUNK_B)
        assert spend_edges.missing_chunks(network, before) == [_CHUNK_B]
        # Spends stored after the bound are this process's own, not history.
        assert spend_edges.missing_chunks(network, before - timedelta(minutes=10)) == []

        spend_edges.backfill_chunk(network, _CHUNK_B)
        assert spend_edges.missing_chunks(network, before) == []

    def test_spends_the_fill_gives_no_edges_are_never_gaps(self, ch):
        network = _network()
        a, b, _ = _addresses(uuid.uuid4().hex[:8])
        before = datetime.now(UTC)
        seen_in_mempool = before - timedelta(minutes=1)
        mempool_only = _in_chunk(
            _tx(network, [_in(a)], [_out(b, 5)], slot=None), _CHUNK_A
        ).model_copy(update={"ingestion_timestamp": seen_in_mempool})
        # Pays only a collateral return, so it pays nothing a hop follows.
        collateral_only = _in_chunk(
            _tx(network, [_in(a)], [_out(b, 5, is_collateral=True)]), _CHUNK_A
        ).model_copy(update={"ingestion_timestamp": seen_in_mempool})
        # Stored before the bound, its edges stamped after it: a transaction
        # seen in the mempool before the start and confirmed after it.
        confirmed = _in_chunk(_tx(network, [_in(a)], [_out(b, 5)]), _CHUNK_B).model_copy(
            update={"ingestion_timestamp": seen_in_mempool}
        )
        ch.insert_transactions_batch([mempool_only, collateral_only, confirmed])
        _drop_chunk_edges(ch, network, _CHUNK_B)
        late = confirmed.model_copy(update={"ingestion_timestamp": before + timedelta(minutes=1)})
        spend_edges.insert_rows(ch._get_client(), spend_edges.rows_for([late], before))

        assert spend_edges.missing_chunks(network, before) == []

        # Not vacuous: the confirmed one is checked, and found once its edges go.
        _drop_chunk_edges(ch, network, _CHUNK_B)
        assert spend_edges.missing_chunks(network, before) == [_CHUNK_B]

    def test_spends_near_their_retention_are_left_out(self, ch, monkeypatch):
        from app.config import settings

        network = _network()
        a, b, _ = _addresses(uuid.uuid4().hex[:8])
        retention_days = 30
        now = datetime.now(UTC)
        expiring = (
            now
            - timedelta(days=retention_days)
            + timedelta(hours=settings.SPEND_EDGES_CHECK_TTL_MARGIN_HOURS / 2)
        )
        tx = _in_chunk(_tx(network, [_in(a)], [_out(b, 5)]), _CHUNK_A).model_copy(
            update={"ingestion_timestamp": expiring}
        )
        ch.insert_transactions_batch([tx])
        _drop_chunk_edges(ch, network, _CHUNK_A)
        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_IO", 0)
        assert spend_edges.missing_chunks(network, now) == [_CHUNK_A], "no retention set"

        monkeypatch.setattr(settings, "CH_RETENTION_DAYS_IO", retention_days)
        assert spend_edges.missing_chunks(network, now) == []

    def test_a_duplicate_refill_changes_no_read(self, ch):
        """The refill rewrites chunks that may still hold edges: the hop query
        and the sandwich history count must read them once."""
        network = _network()
        run = uuid.uuid4().hex[:8]
        a, b, c = _addresses(run)
        script = f"{dex._network_script_prefixes(network)[0]}pool{run}"
        txs = [
            _in_chunk(_tx(network, [_in(a), _in(b)], [_out(b, 5), _out(c, 7)]), _CHUNK_A),
            _in_chunk(_tx(network, [_in(a)], [_out(script, 9), _out(script, 9)]), _CHUNK_A),
            _in_chunk(_tx(network, [_in(a)], [_out(script, 4)], slot=_SLOT + 1), _CHUNK_B),
        ]
        ch.insert_transactions_batch(txs)
        client = ch._get_client()
        hop_params = {
            "addresses": [a, b],
            "network": network,
            "min_slot": _SLOT,
            "max_slot": _SLOT + graph._MAX_AGE_SLOTS,
            "origin_tx": uuid.uuid4().hex * 2,
            "hop_row_limit": graph._BFS_HOP_ROW_LIMIT,
        }
        after_slot = _SLOT + graph._MAX_AGE_SLOTS

        def reads():
            return (
                client.execute(graph._HOP_QUERY, hop_params),
                dex._count_attacker_history(client, a, network, after_slot),
            )

        written = reads()
        assert written[0] and written[1] == 2, "the plant reaches both reads"

        for _ in range(2):
            spend_edges.backfill_chunk(network, _CHUNK_A)
            spend_edges.backfill_chunk(network, _CHUNK_B)

        assert reads() == written
