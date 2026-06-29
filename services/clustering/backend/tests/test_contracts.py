"""Tests for target classification (address vs minting policy).

The on-chain metadata fetch lives with the data-source adapter (app/sources/host_ch);
the ChainSource conformance suite (tests/sources) covers it.
"""

from __future__ import annotations

import pytest

from app.contracts import classify_target

POLICY = "ab" * 28  # 56-hex policy id


def test_classify_target_address() -> None:
    assert classify_target("addr1wxy49hzx86ch868hr3uz98lqw8p7ef55j6x8ras7udy3a0") == "address"


def test_classify_target_policy() -> None:
    assert classify_target(POLICY) == "policy"
    assert classify_target(POLICY.upper()) == "policy"


def test_classify_target_invalid() -> None:
    with pytest.raises(ValueError):
        classify_target("not-an-address")
