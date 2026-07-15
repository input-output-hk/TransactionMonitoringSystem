"""Unit tests for address helpers in app.utils.bech32."""

from __future__ import annotations

import pytest

from app.utils.bech32 import address_network_class

# Real, valid addresses: a mainnet script (Djed v1) and a preprod payment address.
_MAINNET = "addr1wxy49hzx86ch868hr3uz98lqw8p7ef55j6x8ras7udy3a0gm8cdla"
_TESTNET = "addr_test1qz3ql06nvc602eem2af4aefp7w5ce4ja7nuuarzavnkd06ljl64qlwnlynjwzevdrufxslpe29y47u5wxmv6nad026lqvehpe5"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (_MAINNET, "mainnet"),
        (_TESTNET, "testnet"),
        # Not classifiable → None (don't block): Byron-style, junk, truncated.
        ("Ae2tdPwUPEZ4YjgvykNpoFeYUxoyhNj2kg8KfKWN2FizsSpLUPv68MpTVDo", None),
        ("not-an-address", None),
        ("addr_test1qz3ql06nvc602eem2af4aefp7w5ce4ja7nuuarzavnkd06l", None),
    ],
)
def test_address_network_class(address: str, expected: str | None) -> None:
    assert address_network_class(address) == expected
