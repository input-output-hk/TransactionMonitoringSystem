"""Guards for the ``get_source`` dispatch and the per-network Blockfrost URL.

These are the routing determinants the rest of the engine trusts implicitly: the
conformance suite constructs each source directly, so without these the factory
branch and ``BlockfrostSource.host_backed`` (which decides download vs host-backed
in ``pipeline`` / ``select_repo_factory``) would be untested, and a regression
there would leave the suite green while silently producing no/wrong data.
"""

from __future__ import annotations

import pytest

from app.blockfrost.source import BlockfrostSource
from app.config import _BLOCKFROST_BASE_URLS, Settings
from app.sources.factory import get_source
from app.sources.host_ch import HostChainSource


def test_get_source_blockfrost_is_a_download_source() -> None:
    src = get_source(Settings(CHAIN_SOURCE="blockfrost", BLOCKFROST_PROJECT_ID="t"))
    assert isinstance(src, BlockfrostSource)
    # host_backed False is what routes it through the download path (ingest +
    # base ClickHouseRepo); if it flipped True the downloaded rows would never
    # be inserted. See app/service/pipeline.py and select_repo_factory.
    assert src.host_backed is False


def test_get_source_host_ch_is_host_backed() -> None:
    src = get_source(Settings(CHAIN_SOURCE="host_ch"))
    assert isinstance(src, HostChainSource)
    assert src.host_backed is True


def test_get_source_is_case_and_whitespace_insensitive() -> None:
    src = get_source(Settings(CHAIN_SOURCE=" Blockfrost ", BLOCKFROST_PROJECT_ID="t"))
    assert isinstance(src, BlockfrostSource)


def test_get_source_unknown_raises() -> None:
    with pytest.raises(ValueError):
        get_source(Settings(CHAIN_SOURCE="nope"))


@pytest.mark.parametrize("network", sorted(_BLOCKFROST_BASE_URLS))
def test_blockfrost_base_url_resolves_per_network(network: str) -> None:
    # Assert the mapping is present and well-formed for each network without
    # duplicating the literal URL (CLAUDE.md: don't restate config values). A
    # per-network entry must be an HTTPS Blockfrost API base naming that network.
    url = Settings(CARDANO_NETWORK=network).blockfrost_base_url
    assert url.startswith("https://") and url.endswith("/api/v0")
    assert network in url


def test_blockfrost_base_url_unknown_network_raises() -> None:
    with pytest.raises(ValueError):
        _ = Settings(CARDANO_NETWORK="does-not-exist").blockfrost_base_url
