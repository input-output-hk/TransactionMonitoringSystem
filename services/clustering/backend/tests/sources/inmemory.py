"""In-memory reference ``ChainSource`` for the test suite.

A self-contained, page-based data source that implements the full ``ChainSource``
protocol over in-memory fixtures (no HTTP, no provider client). It carries the
generic page-cursor discovery algorithm a *query-by-target* adapter is expected
to implement:

  * ``page:N`` resume cursors and ``page:N;from:H`` recent-window anchoring (the
    engine treats the whole string as opaque; only a source parses it);
  * asc/desc paging with ``max_items`` and block-range filtering;
  * the neutral ``SourceError`` taxonomy (rate-limit / not-found).

It is the conformance fixture for the ``ChainSource`` seam (see ``harness.py``)
and drives the ingester's characterization tests (``test_ingester.py``) exactly
as a real downloading adapter would, without pinning the suite to any one
provider's wire format.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.models import TxRecord, UtxoRecord
from app.sources.base import (
    DiscoveryMode,
    DiscoveryWindow,
    NormalizedTx,
    SourceNotFound,
    SourceRateLimited,
    TargetMeta,
)

# Page-cursor encoding: the 1-based number of the last fully-ingested discovery
# page, with the recent-window block-height anchor appended (``page:3;from:40``)
# so a resumed walk pages through the same ``from=``-filtered set. A generic
# page-based convention, identical to what any such adapter would persist.
_CURSOR_PREFIX = "page:"
_ANCHOR_SEP = ";from:"

# Per-tx record template values (the discovery/cursor behaviour under test does
# not depend on the economic fields; one input + one output keeps the counts
# self-consistent for the conformance assertions).
_FIXED_TIME = datetime(2023, 11, 14, tzinfo=UTC)  # ~ block_time 1_700_000_000
_INPUT_LOVELACE = 1_000_000
_OUTPUT_LOVELACE = 900_000


def _page_cursor(page: int, anchor: str | None = None) -> str:
    base = f"{_CURSOR_PREFIX}{page}"
    return f"{base}{_ANCHOR_SEP}{anchor}" if anchor else base


def _parse_cursor(cursor: str | None) -> tuple[int, str | None]:
    """``(page, from_block_anchor)`` a cursor names, or ``(0, None)`` for a
    None/garbled cursor (which then behaves like ``restart``)."""
    if not cursor or not cursor.startswith(_CURSOR_PREFIX):
        return 0, None
    body = cursor[len(_CURSOR_PREFIX) :]
    page_part, sep, anchor = body.partition(_ANCHOR_SEP)
    try:
        page = max(0, int(page_part))
    except ValueError:
        return 0, None
    if not sep:
        return page, None
    if not anchor.isdecimal() or int(anchor) <= 0:
        return 0, None
    return page, anchor


def _start_page(page: int, mode: DiscoveryMode) -> int:
    """Where a walk begins for a (cursor page, mode) pair. ``resume`` continues
    after the cursor's page; ``tip`` re-fetches it (idempotent catch-up);
    ``restart`` ignores it."""
    if mode == "resume":
        return page + 1
    if mode == "tip":
        return max(1, page)
    return 1


@dataclass(slots=True)
class InMemoryChainSource:
    """A ``ChainSource`` backed entirely by in-memory fixtures.

    ``address_txs`` maps an address to its ``[(tx_hash, block_height)]`` history
    (ascending by height). ``policy_assets`` maps a policy id to its asset units,
    and ``asset_txs`` maps an asset unit to the tx hashes touching it (duplicates
    across assets are deduped by discovery, mirroring a real policy walk). The
    ``rate_limited_*`` / ``missing_txs`` sets inject the neutral error conditions a
    conformance suite must exercise.
    """

    address_txs: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    policy_assets: dict[str, list[str]] = field(default_factory=dict)
    asset_txs: dict[str, list[str]] = field(default_factory=dict)
    page_size: int = 2
    # Addresses whose DESC listing hits the daily limit (pins the recent-window
    # pre-walk's 402 while leaving the ASC history walk usable).
    rate_limited_desc: frozenset[str] = frozenset()
    # Addresses/assets whose listing hits the daily limit during discovery.
    rate_limited_listings: frozenset[str] = frozenset()
    # Tx hashes whose per-tx fetch raises rate-limited / not-found.
    rate_limited_txs: frozenset[str] = frozenset()
    missing_txs: frozenset[str] = frozenset()
    script_addresses: frozenset[str] = frozenset()

    async def __aenter__(self) -> InMemoryChainSource:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    # --- internal paged listings (mirror a page-based list endpoint) ----------

    async def _address_listing(
        self,
        address: str,
        *,
        order: str = "asc",
        start_page: int = 1,
        max_items: int | None = None,
        from_block: str | None = None,
        to_block: str | None = None,
    ) -> AsyncIterator[tuple[int, list[dict[str, object]]]]:
        if order == "desc" and address in self.rate_limited_desc:
            raise SourceRateLimited(f"daily limit on desc listing for {address}")
        if address in self.rate_limited_listings:
            raise SourceRateLimited(f"daily limit during discovery for {address}")
        txs = self.address_txs.get(address, [])
        if from_block is not None:
            txs = [t for t in txs if t[1] >= int(from_block)]
        if to_block is not None:
            txs = [t for t in txs if t[1] <= int(to_block)]
        seq = txs[::-1] if order == "desc" else list(txs)
        page = start_page
        yielded = 0
        while True:
            chunk = seq[(page - 1) * self.page_size : page * self.page_size]
            if not chunk:
                return
            yield page, [{"tx_hash": h, "block_height": bh} for h, bh in chunk]
            yielded += len(chunk)
            if (max_items is not None and yielded >= max_items) or len(chunk) < self.page_size:
                return
            page += 1

    async def _asset_listing(self, asset: str) -> AsyncIterator[list[dict[str, str]]]:
        if asset in self.rate_limited_listings:
            raise SourceRateLimited(f"daily limit on asset listing for {asset}")
        seq = self.asset_txs.get(asset, [])
        page = 1
        while True:
            chunk = seq[(page - 1) * self.page_size : page * self.page_size]
            if not chunk:
                return
            yield [{"tx_hash": h} for h in chunk]
            if len(chunk) < self.page_size:
                return
            page += 1

    # --- discovery -------------------------------------------------------------

    async def _recent_anchor(
        self,
        address: str,
        n: int,
        progress: Callable[[str], None],
        *,
        to_block: str | None = None,
    ) -> str | None:
        """Block height of the address's nth-newest transaction (a cheap desc
        walk), or None when there are fewer than ``n`` txs. With ``to_block``
        the anchor names the nth-newest tx AT OR BELOW that height (the
        reference behavior for a bounded recent window)."""
        count = 0
        async for _page, items in self._address_listing(
            address, order="desc", max_items=n, to_block=to_block
        ):
            if count + len(items) >= n:
                height = items[n - 1 - count].get("block_height")
                if not height:
                    return None
                progress(f"recent window: anchoring at block {height} ({n} most recent txs)")
                return str(height)
            count += len(items)
        return None

    async def _address_pages(
        self,
        address: str,
        *,
        start_page: int,
        max_items: int | None,
        from_block: str | None,
        to_block: str | None,
        anchor: str | None = None,
    ) -> AsyncIterator[tuple[str, list[str]]]:
        async for page, items in self._address_listing(
            address,
            order="asc",
            start_page=start_page,
            max_items=max_items,
            from_block=from_block if from_block is not None else anchor,
            to_block=to_block,
        ):
            yield _page_cursor(page, anchor), [str(it["tx_hash"]) for it in items]

    async def _policy_tx_hashes(self, policy_id: str) -> AsyncIterator[str]:
        for asset in self.policy_assets.get(policy_id, []):
            async for txs in self._asset_listing(asset):
                for tx in txs:
                    yield tx["tx_hash"]

    async def _policy_pages(
        self,
        policy_id: str,
        *,
        start_page: int,
        max_items: int | None,
        progress: Callable[[str], None],
    ) -> AsyncIterator[tuple[str, list[str]]]:
        seen: set[str] = set()
        buf: list[str] = []
        page = 0

        def _flush() -> tuple[str, list[str]] | None:
            nonlocal buf, page
            if not buf:
                return None
            page += 1
            out = (_page_cursor(page), buf) if page >= start_page else None
            buf = []
            return out

        async for h in self._policy_tx_hashes(policy_id):
            if h in seen:
                continue
            seen.add(h)
            buf.append(h)
            if len(buf) >= self.page_size:
                emitted = _flush()
                if emitted is not None:
                    yield emitted
            if max_items is not None and len(seen) >= max_items:
                break

        tail = _flush()
        if tail is not None:
            yield tail
        progress(f"policy {policy_id[:12]}…: discovered {len(seen)} unique transactions")

    async def tx_hash_pages(
        self,
        *,
        address: str | None,
        policy_id: str | None,
        cursor: str | None,
        mode: DiscoveryMode,
        max_items: int | None,
        from_block: str | None,
        to_block: str | None,
        window: DiscoveryWindow = "history",
        progress: Callable[[str], None],
    ) -> AsyncIterator[tuple[str, list[str]]]:
        page, anchor = _parse_cursor(cursor)
        # An explicit block range overrides a stored anchor (page numbers would
        # name positions in a different, anchor-filtered result set).
        if from_block is not None:
            anchor = None
        start_page = _start_page(page, mode)
        if address is not None:
            # (Re)anchor a capped walk to the recent window on a fresh/empty cursor
            # or a restart (re-anchoring lower widens the window); a mid-walk resume
            # keeps its stored anchor so filtered page numbers stay aligned.
            if (
                window == "recent"
                and max_items
                and from_block is None
                and mode != "tip"
                and ((page, anchor) == (0, None) or mode == "restart")
            ):
                # to_block composes with the recent window (anchor found inside
                # the bounded set) — mirrors the Blockfrost adapter's behavior.
                anchor = await self._recent_anchor(
                    address, max_items, progress, to_block=to_block
                )
            pages = self._address_pages(
                address,
                start_page=start_page,
                max_items=max_items,
                from_block=from_block,
                to_block=to_block,
                anchor=anchor,
            )
        else:
            assert policy_id is not None
            pages = self._policy_pages(
                policy_id, start_page=start_page, max_items=max_items, progress=progress
            )
        async for page_out in pages:
            yield page_out

    # --- per-tx fetch + metadata ----------------------------------------------

    async def fetch_tx(self, target: str, target_type: str, tx_hash: str) -> NormalizedTx:
        if tx_hash in self.missing_txs:
            raise SourceNotFound(f"tx {tx_hash} not found")
        if tx_hash in self.rate_limited_txs:
            raise SourceRateLimited(f"daily limit on fetch of {tx_hash}")
        tx = TxRecord(
            target=target,
            target_type=target_type,
            tx_hash=tx_hash,
            block_height=1,
            block_time=_FIXED_TIME,
            slot=1,
            fees=200_000,
            deposit=0,
            size=300,
            valid_contract=1,
            input_count=1,
            output_count=1,
            total_input_lovelace=_INPUT_LOVELACE,
            total_output_lovelace=_OUTPUT_LOVELACE,
            distinct_input_addresses=1,
            distinct_output_addresses=1,
            distinct_assets=0,
            redeemer_count=0,
        )
        utxos = [
            UtxoRecord(target, tx_hash, "input", 0, "addrIn", _INPUT_LOVELACE),
            UtxoRecord(target, tx_hash, "output", 0, "addrOut", _OUTPUT_LOVELACE),
        ]
        return NormalizedTx(tx, utxos, [])

    async def metadata(self, target: str, target_type: str) -> TargetMeta:
        return {
            "exists": 1,
            "is_script": int(target_type == "policy" or target in self.script_addresses),
            "script_type": "",
            "balance_lovelace": 5_000_000,
            "asset_count": 0,
            "sample_tokens": "[]",
        }
