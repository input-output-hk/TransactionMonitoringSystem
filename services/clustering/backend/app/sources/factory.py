"""Selects the configured ``ChainSource`` implementation.

The pipeline and CLI ask here for a source instead of constructing a provider
client directly, so swapping Blockfrost for a node is a one-line config change
plus a new adapter."""

from __future__ import annotations

from app.config import Settings
from app.sources.base import ChainSource


def get_source(settings: Settings) -> ChainSource:
    """Return the data source named by ``settings.chain_source``."""
    name = settings.chain_source.strip().lower()
    if name == "blockfrost":
        # Imported lazily so the (future) node adapter doesn't drag in Blockfrost.
        from app.blockfrost.source import BlockfrostSource

        return BlockfrostSource(settings)
    if name == "host_ch":
        # The integrated TMS clustering sidecar: discovery/metadata from the
        # host's already-ingested ClickHouse, no download (pair with HostBackedRepo).
        from app.sources.host_ch import HostChainSource

        return HostChainSource(settings)
    raise ValueError(f"unknown CHAIN_SOURCE {settings.chain_source!r}")
