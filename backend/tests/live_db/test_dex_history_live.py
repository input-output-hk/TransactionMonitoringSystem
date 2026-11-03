"""Live-ClickHouse tests for the sandwich attacker-history count.

The count moved from a join of transaction_inputs and transaction_outputs
(which built every script output of the chain in memory, 1.4 to 2.2 GB per call
on mainnet) to a range read of spend_edges. spend_edges has every spending
input, not only the first, so the new count must never fall below the old one:
the recurrence sub-score may rise, never drop. Requires TMS_LIVE_DB_TESTS=1.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.analysis import dex
from app.models.transaction import NormalizedTransaction, TransactionInput, TransactionOutput

from .conftest import LIVE_NETWORK

_SLOT = 4_000_000
# The slot the count is taken at: the plant's "later" spend sits past it.
_NOW_SLOT = _SLOT + 5
_LATER_SLOT = _NOW_SLOT + 5

# The query this replaced, kept to hold the new count to it.
_LEGACY_HISTORY = """
    SELECT count(DISTINCT i.tx_hash)
    FROM transaction_inputs i
    JOIN transaction_outputs o ON i.tx_hash = o.tx_hash AND i.network = o.network
    WHERE i.network = %(network)s
      AND i.address = %(addr)s
      AND i.input_index = 0
      AND i.is_collateral = 0
      AND i.is_reference = 0
      AND i.is_unspent_attempt = 0
      AND o.is_collateral = 0
      AND ({like_clause})
      AND i.tx_hash IN (
          SELECT tx_hash FROM transactions
          WHERE network = %(network)s AND slot < %(slot)s
      )
"""


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


def _out(address, amount=5_000_000) -> TransactionOutput:
    return TransactionOutput(address=address, amount=amount)


def _legacy(ch, network, attacker) -> int:
    prefixes = dex._network_script_prefixes(network)
    like = " OR ".join(f"o.address LIKE %(p{i})s" for i in range(len(prefixes)))
    params = {"network": network, "addr": attacker, "slot": _NOW_SLOT}
    params.update({f"p{i}": f"{prefix}%" for i, prefix in enumerate(prefixes)})
    return ch._get_client().execute(_LEGACY_HISTORY.format(like_clause=like), params)[0][0]


@pytest.fixture
def planted(ch):
    network = f"{LIVE_NETWORK}-dex-{uuid.uuid4().hex[:8]}"
    run = uuid.uuid4().hex[:8]
    script = f"{dex._network_script_prefixes(network)[0]}pool{run}"
    attacker, other, wallet = (f"addr1q{name}{run}" for name in ("attacker", "other", "wallet"))
    ch.insert_transactions_batch(
        [
            # Its first input, paying a script: both counts.
            _tx(network, [_in(attacker)], [_out(script)]),
            # Its second input, paying a script: only the spend_edges count.
            _tx(network, [_in(other), _in(attacker)], [_out(script)]),
            # Paying a wallet, not a script: neither.
            _tx(network, [_in(attacker)], [_out(wallet)]),
            # Its collateral, which spends nothing: neither.
            _tx(network, [_in(attacker, is_collateral=True), _in(other)], [_out(script)]),
            # After the slot the count is taken at: neither.
            _tx(network, [_in(attacker)], [_out(script)], slot=_LATER_SLOT),
        ]
    )
    return network, attacker


def test_the_count_is_never_below_the_first_input_one(ch, planted):
    network, attacker = planted
    client = ch._get_client()

    assert _legacy(ch, network, attacker) == 1
    assert dex._count_attacker_history(client, attacker, network, _NOW_SLOT) == 2


def test_the_count_stops_at_its_cap(ch, planted, monkeypatch):
    network, attacker = planted
    monkeypatch.setattr(dex, "_HISTORY_COUNT_CAP", 1)

    assert dex._count_attacker_history(ch._get_client(), attacker, network, _NOW_SLOT) == 1
