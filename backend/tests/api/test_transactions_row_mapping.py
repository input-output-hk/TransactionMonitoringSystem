"""Pin the positional row -> TransactionResponse field mapping.

``_row_to_transaction`` is the single field-to-index contract shared by the
list and detail handlers. A reordered SELECT column would otherwise silently
misalign a field with no type error, so this locks the index of each field.
"""

from datetime import UTC, datetime

from app.api.transactions import TransactionResponse, _row_to_transaction


def _row():
    # Each position holds a distinguishable, type-valid value so a swapped index
    # is caught by an assertion below.
    return [
        "a" * 64,                        # 0 tx_hash
        11,                              # 1 slot
        22,                              # 2 block_height
        "blockhash",                     # 3 block_hash
        33,                              # 4 block_index
        datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),  # 5 timestamp
        66,                              # 6 fee
        77,                              # 7 deposit
        8,                               # 8 input_count
        9,                               # 9 output_count
        1010,                            # 10 total_input_value
        1111,                            # 11 total_output_value
        ["addr1", "addr2"],              # 12 addresses
    ]


def test_row_to_transaction_maps_every_field_by_index():
    row = _row()
    tx = _row_to_transaction(row)
    assert isinstance(tx, TransactionResponse)
    assert tx.tx_hash == row[0]
    assert tx.slot == row[1]
    assert tx.block_height == row[2]
    assert tx.block_hash == row[3]
    assert tx.block_index == row[4]
    assert tx.timestamp == row[5]
    assert tx.fee == row[6]
    assert tx.deposit == row[7]
    assert tx.input_count == row[8]
    assert tx.output_count == row[9]
    assert tx.total_input_value == row[10]
    assert tx.total_output_value == row[11]
    assert tx.addresses == row[12]


def test_row_to_transaction_falsy_addresses_become_empty_list():
    row = _row()
    row[12] = None
    assert _row_to_transaction(row).addresses == []
