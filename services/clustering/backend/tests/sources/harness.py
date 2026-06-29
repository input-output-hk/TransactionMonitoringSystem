"""Conformance harness: the executable spec every ``ChainSource`` adapter must pass.

Wire fixtures can't be shared across providers (a node's CBOR vs a REST API's
JSON), so the suite is *scenario-driven*: each adapter registers a factory that
builds a fresh source preloaded with the canonical scenario below, and
``test_conformance.py`` asserts purely in protocol terms (hashes, cursors,
``SourceError`` taxonomy, ``NormalizedTx`` consistency, ``TargetMeta`` keys).

The reference adapter is the in-memory ``InMemoryChainSource`` (no network), which
both pins the protocol's expected behaviour and exercises the ingester. A real
adapter for a query-by-target provider proves itself against the same spec.

Porting checklist for a new adapter (Kupo / db-sync / ...):
  1. Build a stubbed client serving the scenario's data in your wire format.
  2. Register a factory in ``SOURCE_FACTORIES`` returning (source, Scenario).
  3. Run ``pytest tests/sources`` — green means the engine can drive you.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.sources.base import ChainSource
from tests.sources.inmemory import InMemoryChainSource


@dataclass(slots=True)
class Scenario:
    """What the canonical scenario contains, in protocol terms."""

    target: str
    target_type: str  # 'address' | 'policy'
    expected_hashes: list[str]  # full discovery result, in the source's stable order
    missing_tx: str  # fetch_tx must raise SourceNotFound
    rate_limited_target: str  # discovery on THIS target must raise SourceRateLimited
    # Expected hashes for window="recent" with max_items=len(recent_window).
    # None = the adapter ignores the hint (suite then asserts history behaviour).
    recent_window: list[str] | None = None


SourceFactory = Callable[[], tuple[ChainSource, Scenario]]


# --- Canonical scenario -------------------------------------------------------

# Five txs over three pages (page size 2) — enough to cut a resume anywhere.
# Distinct, ascending block heights so the recent-window pre-walk can anchor
# mid-history.
_HASHES = ["aa", "bb", "cc", "dd", "ee"]
_HEIGHTS = {h: (i + 1) * 10 for i, h in enumerate(_HASHES)}
_PAGE_SIZE = 2

_SCENARIO = Scenario(
    target="addr1conformance",
    target_type="address",
    expected_hashes=list(_HASHES),
    missing_tx="feedbead",
    rate_limited_target="addr1limited",
    recent_window=["cc", "dd", "ee"],
)


def _make_inmemory() -> tuple[ChainSource, Scenario]:
    source = InMemoryChainSource(
        address_txs={_SCENARIO.target: [(h, _HEIGHTS[h]) for h in _HASHES]},
        page_size=_PAGE_SIZE,
        rate_limited_listings=frozenset({_SCENARIO.rate_limited_target}),
        missing_txs=frozenset({_SCENARIO.missing_tx}),
        script_addresses=frozenset({_SCENARIO.target}),
    )
    return source, _SCENARIO


SOURCE_FACTORIES: dict[str, SourceFactory] = {
    "inmemory": _make_inmemory,
    # "kupo": _make_kupo,        <- future adapters register here
    # "dbsync": _make_dbsync,
}
