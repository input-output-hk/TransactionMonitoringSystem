"""Host-backed ChainSource: discovery + metadata read from the host TMS's
already-ingested ClickHouse (CHAIN_SOURCE=host_ch). No Blockfrost, no download."""

from app.sources.host_ch.source import HostChainSource

__all__ = ["HostChainSource"]
