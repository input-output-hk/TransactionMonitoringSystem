"""The block writer commits a transaction's transactions row last.

The scoring lanes pick from transactions, so a visible transactions row must
mean the block's inputs, outputs and feature rows are already committed: a
transaction picked before its outputs landed is stored with circular and
sandwich at -1 and never picked again. Pinned against a mocked client.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.db import clickhouse
from app.models.transaction import NormalizedTransaction, TransactionInput, TransactionOutput


@pytest.fixture
def client(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(clickhouse, "_get_client", lambda: mock)
    return mock


def _tx() -> NormalizedTransaction:
    return NormalizedTransaction(
        tx_hash="ab" * 32,
        network="preprod",
        slot=1,
        block_height=1,
        block_hash="cd" * 32,
        block_index=0,
        timestamp=datetime.now(UTC),
        fee=1,
        inputs=[TransactionInput(tx_hash="ef" * 32, index=0, address="addr_test1qqin", amount=2)],
        outputs=[TransactionOutput(address="addr_test1qqout", amount=1)],
        input_count=1,
        output_count=1,
        addresses=["addr_test1qqin", "addr_test1qqout"],
    )


def _inserted_tables(mock) -> list[str]:
    return [
        c.args[0].split("INSERT INTO")[1].split("(")[0].strip()
        for c in mock.execute.call_args_list
        if "INSERT INTO" in c.args[0]
    ]


def test_the_transactions_row_is_written_after_every_other_row(client):
    clickhouse.insert_transactions_batch([_tx()])
    tables = _inserted_tables(client)
    assert tables[-1] == "transactions"
    assert {"transaction_inputs", "transaction_outputs"} <= set(tables[:-1])


def test_a_failure_before_it_leaves_no_transactions_row(client):
    # The block's retry or replay writes everything again; until then no lane
    # can pick a transaction whose outputs are missing.
    def fail_on_outputs(query, *args, **kwargs):
        if "INSERT INTO transaction_outputs" in query:
            raise clickhouse.ClickHouseError("timeout")

    client.execute.side_effect = fail_on_outputs
    with pytest.raises(clickhouse.ClickHouseError):
        clickhouse.insert_transactions_batch([_tx()])
    assert "transactions" not in _inserted_tables(client)
