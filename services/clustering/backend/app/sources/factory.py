"""Selects the configured ``ChainSource`` implementation.

The pipeline and CLI ask here for a source instead of constructing a provider
client directly, so adding a node/db-sync adapter is a one-line config change
plus a new class behind the ``ChainSource`` protocol."""

from __future__ import annotations

from app.config import Settings
from app.sources.base import ChainSource


def get_source(settings: Settings) -> ChainSource:
    """Return the data source named by ``settings.chain_source``."""
    name = settings.chain_source.strip().lower()
    if name == "host_ch":
        # The integrated TMS clustering sidecar: discovery/metadata from the
        # host's already-ingested ClickHouse, no download (pair with HostBackedRepo).
        from app.sources.host_ch import HostChainSource

        return HostChainSource(settings)
    if name == "blockfrost":
        # On-demand download of an arbitrary address's history over HTTP (no local
        # index, no extra disk). Imported lazily so host_ch deployments don't drag
        # in httpx/Blockfrost. Not host-backed: routes through the download path.
        from app.blockfrost.source import BlockfrostSource

        return BlockfrostSource(settings)
    raise ValueError(f"unknown CHAIN_SOURCE {settings.chain_source!r}")
