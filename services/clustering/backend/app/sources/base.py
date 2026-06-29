"""The ``ChainSource`` protocol, its normalized return types, and the
provider-neutral error hierarchy the engine depends on instead of any provider
package."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from app.models import AssetRecord, TxRecord, UtxoRecord

# How a discovery walk relates to a previously-persisted cursor:
#   resume  — continue AFTER the cursor (the page it names is already ingested);
#   tip     — incremental catch-up: re-cover the cursor's position so anything
#             appended since is picked up (re-fetching is idempotent);
#   restart — ignore the cursor and walk from the beginning.
# The ingester picks the mode (provider-neutral logic); only the source knows
# what the cursor means.
DiscoveryMode = Literal["resume", "tip", "restart"]

# Which slice of the target's history a capped walk should cover:
#   history — from the beginning (the default, and the only option when unbounded);
#   recent  — the most recent ``max_items`` txs, so a capped onboard reflects
#             current traffic and leaves the cursor near the tip.
# A hint, not a contract: it only applies when ``max_items`` is set and the walk
# starts fresh (no usable cursor); sources that cannot honor it (e.g. asset-driven
# policy discovery) behave as ``history``.
DiscoveryWindow = Literal["history", "recent"]


class SourceError(RuntimeError):
    """Base error for chain data-source failures (provider-neutral)."""


class SourceNotFound(SourceError):
    """The requested address/policy/asset/transaction does not exist upstream."""


class SourceRateLimited(SourceError):
    """The source's request quota/rate limit was hit; the run should stop and
    resume later (the ingester persists its cursor when it sees this)."""


@dataclass(slots=True)
class NormalizedTx:
    """One transaction mapped into the storage layer's neutral row records.

    Sources own their own wire-format → record normalization and return this, so
    the ingester never sees a provider-specific payload.
    """

    tx: TxRecord
    utxos: list[UtxoRecord] = field(default_factory=list)
    assets: list[AssetRecord] = field(default_factory=list)


# Contract identity/metadata fields the ``contracts`` table expects, as a plain
# dict so the pipeline's ``contract.update(meta)`` stays unchanged. Keys:
# ``exists``, ``is_script``, ``script_type``, ``balance_lovelace``,
# ``asset_count``, ``sample_tokens`` (a JSON-encoded ``[{unit, policy_id, name}]``).
TargetMeta = dict[str, Any]


@runtime_checkable
class ChainSource(Protocol):
    """Everything the ingester/pipeline needs from a *historical* (query-by-target)
    chain data provider.

    Implementations own their wire-format → domain-record normalization and
    translate provider errors into the ``SourceError`` hierarchy, so no analysis
    code imports a provider package. ``host_ch`` (reading the host TMS's ingested
    ClickHouse) is the implementation today; a Kupo/db-sync adapter drops in behind
    this protocol. A *streaming* node source (Ogmios chainsync) is a different shape
    (it pushes whole blocks and rollbacks rather than answering queries) and gets
    its own ``TipSource`` protocol when implemented; see
    docs/online-classification-design.md ("Node-fed ingestion").

    Cursors are **owned by the source**: an opaque-but-tagged string (a page-based
    adapter: ``"page:42"``, or ``"page:42;from:10422911"`` when the walk is anchored
    to a recent window; a node adapter: ``"point:<slot>.<block_hash>"``). The engine
    persists and replays them verbatim, never doing arithmetic on them, and the
    stored cursor is only replayed into the same ``CHAIN_SOURCE`` that produced it.
    """

    # Whether this source's data already lives in storage the engine reads
    # directly (so onboarding must NOT discover+download individual txs). A
    # host-backed source (``host_ch``, reading the host TMS's ingested tables via
    # ``HostBackedRepo``) sets this ``True`` and has no ``fetch_tx``: the canonical
    # fit reads features straight from the host tables, so the pipeline skips the
    # download path for it regardless of the per-job ``reprocess`` flag. A
    # downloading adapter (Kupo/db-sync) leaves it ``False`` (the default).
    host_backed: bool = False

    async def __aenter__(self) -> ChainSource: ...

    async def __aexit__(self, *exc: object) -> None: ...

    def tx_hash_pages(
        self,
        *,
        address: str | None,
        policy_id: str | None,
        cursor: str | None,
        mode: DiscoveryMode,
        max_items: int | None,
        from_block: str | None,
        to_block: str | None,
        window: DiscoveryWindow,
        progress: Callable[[str], None],
    ) -> AsyncIterator[tuple[str, list[str]]]:
        """Pages ``(resume_cursor, [tx_hash])`` touching the target, in a stable
        order. ``resume_cursor`` is the cursor to persist once that page's txs are
        ingested — replaying it with ``mode="resume"`` continues exactly after the
        page (no gaps, no duplicates), so the caller never computes cursors itself.
        ``cursor``/``mode`` say where to start (see ``DiscoveryMode``); ``window``
        says which slice a capped fresh walk covers (see ``DiscoveryWindow``).
        Raises ``SourceRateLimited`` if the provider quota is hit mid-discovery."""
        ...

    async def fetch_tx(self, target: str, target_type: str, tx_hash: str) -> NormalizedTx:
        """One transaction, already normalized. Raises ``SourceNotFound`` if the tx
        is absent and ``SourceRateLimited`` on quota exhaustion."""
        ...

    async def metadata(self, target: str, target_type: str) -> TargetMeta:
        """Identity/balance/token metadata for contract onboarding. Raises
        ``SourceNotFound`` if the address/policy does not exist."""
        ...
