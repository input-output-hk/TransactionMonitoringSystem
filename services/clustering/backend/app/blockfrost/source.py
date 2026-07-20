"""Blockfrost implementation of the ``ChainSource`` protocol.

Wraps ``AsyncBlockfrostClient`` and owns everything Blockfrost-specific: page-based
tx discovery, the JSON → record normalization (``app.blockfrost.normalize``),
metadata fetching, and translation of Blockfrost errors into the neutral
``SourceError`` hierarchy. Sunsetting Blockfrost = delete this package and drop it
from ``app.sources.factory``; no analysis code changes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.blockfrost.client import (
    AsyncBlockfrostClient,
    BlockfrostDailyLimitError,
    BlockfrostError,
    BlockfrostNotFoundError,
)
from app.blockfrost.normalize import _lovelace, build_records
from app.config import Settings
from app.sources.base import (
    DiscoveryMode,
    DiscoveryWindow,
    NormalizedTx,
    SourceError,
    SourceNotFound,
    SourceRateLimited,
    TargetMeta,
)

# Cap the per-contract metadata work so onboarding stays fast and cheap.
_SAMPLE_LIMIT = 8
_POLICY_ASSET_CAP = 1000

# Blockfrost's cursor encoding: the 1-based page number of the last fully
# ingested discovery page, tagged for debuggability. Address walks anchored to a
# recent window append the block-height anchor (``page:3;from:10422911``) because
# Blockfrost page numbers are relative to the ``from=``-filtered result set — the
# anchor must travel with the page number or a later walk would count pages of a
# different (unfiltered) set. The engine treats the whole string as opaque; only
# this adapter parses or produces it.
_CURSOR_PREFIX = "page:"
_ANCHOR_SEP = ";from:"


def _page_cursor(page: int, anchor: str | None = None) -> str:
    base = f"{_CURSOR_PREFIX}{page}"
    return f"{base}{_ANCHOR_SEP}{anchor}" if anchor else base


def _parse_cursor(cursor: str | None) -> tuple[int, str | None]:
    """``(page, from_block_anchor)`` a cursor names, or ``(0, None)`` for
    None/foreign/garbled cursors (which then behave like ``restart`` — safe, just
    re-walks). A garbled *anchor* also yields ``(0, None)``: continuing at page N
    of the unfiltered set would be a different position and could skip data."""
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
    # isdecimal, not isdigit: isdigit accepts characters int() rejects (e.g. "³").
    if not anchor.isdecimal() or int(anchor) <= 0:
        return 0, None
    return page, anchor


def _start_page(page: int, mode: DiscoveryMode) -> int:
    """Where a walk begins for a (cursor page, mode) pair — the page arithmetic
    that used to live in the engine. ``resume`` continues after the cursor's page;
    ``tip`` re-fetches it (cheap + idempotent) so txs appended to that page since
    the last walk are picked up; ``restart`` ignores it."""
    if mode == "resume":
        return page + 1
    if mode == "tip":
        return max(1, page)
    return 1


def _asset_name(asset: dict[str, Any], unit: str) -> str:
    """Best-effort human name: on-chain/registry metadata, else hex-decoded name."""
    onchain = asset.get("onchain_metadata") or {}
    registry = asset.get("metadata") or {}
    name = onchain.get("name") or registry.get("name")
    if name:
        return str(name)
    hex_name = asset.get("asset_name") or unit[56:]
    if not hex_name:
        return ""
    try:
        return bytes.fromhex(hex_name).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return hex_name


async def _sample_tokens(client: AsyncBlockfrostClient, units: list[str]) -> list[dict[str, str]]:
    """Resolve a capped sample of asset units to ``{unit, policy_id, name}``."""
    out: list[dict[str, str]] = []
    for unit in units:
        try:
            asset = await client.asset(unit)
        except BlockfrostNotFoundError:
            continue
        out.append({"unit": unit, "policy_id": unit[:56], "name": _asset_name(asset, unit)})
    return out


async def fetch_contract_metadata(
    client: AsyncBlockfrostClient, target: str, target_type: str
) -> dict[str, Any]:
    """Fetch identity + balance + token metadata for a target.

    Raises ``BlockfrostNotFoundError`` if the address/policy does not exist (the
    ``BlockfrostSource`` wrapper translates that to ``SourceNotFound``). Returns a
    dict whose keys match the ``contracts`` columns the storage layer expects.
    """
    if target_type == "address":
        info = await client.address_info(target)
        amount = info.get("amount", [])
        units = [e["unit"] for e in amount if e.get("unit") != "lovelace"]
        return {
            "exists": 1,
            "is_script": int(bool(info.get("script"))),
            "script_type": "",
            "balance_lovelace": _lovelace(amount),
            "asset_count": len(units),
            "sample_tokens": json.dumps(await _sample_tokens(client, units[:_SAMPLE_LIMIT])),
        }

    # policy target
    script_info = await client.script(target)
    units = []
    async for assets in client.policy_assets(target):
        units.extend(a["asset"] for a in assets)
        if len(units) >= _POLICY_ASSET_CAP:
            break
    return {
        "exists": 1,
        "is_script": 1,
        "script_type": str(script_info.get("type", "") or ""),
        "balance_lovelace": 0,
        "asset_count": len(units),
        "sample_tokens": json.dumps(await _sample_tokens(client, units[:_SAMPLE_LIMIT])),
    }


class BlockfrostSource:
    """``ChainSource`` backed by ``AsyncBlockfrostClient``.

    A pre-built client may be injected (tests use an ``httpx.MockTransport``);
    otherwise one is constructed from ``settings``. The context-manager protocol
    delegates to the underlying client's lifecycle.
    """

    # A downloading adapter: discovery + per-tx fetch feed the ingester's download
    # path (writes into the engine's own tables), unlike a host-backed source that
    # reads the host's already-ingested tables. Satisfies the ChainSource protocol
    # member and routes ``select_repo_factory`` to the base (inserting) repo.
    host_backed = False

    # Cursor tag (see ChainSource.name) for this source's PRIMARY-mode use
    # (CHAIN_SOURCE=blockfrost). The pre-deployment history flavor overrides
    # this per-instance to a distinct tag (service/history.py) before handing
    # the source to ingest(): its bounded walk's cursor must never collide with
    # an unbounded primary walk's, e.g. across a CHAIN_SOURCE migration that
    # left old cursors on an un-wiped volume.
    name = "blockfrost"

    def __init__(self, settings: Settings, *, client: AsyncBlockfrostClient | None = None) -> None:
        self._settings = settings
        self._client = client if client is not None else AsyncBlockfrostClient(settings)

    async def __aenter__(self) -> BlockfrostSource:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)

    # --- Discovery -------------------------------------------------------------

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
        """Asc walk of the address's transactions. With ``anchor`` set, the walk is
        filtered to ``from=anchor`` and every yielded cursor carries the anchor, so
        resume/tip walks page through the same filtered (append-only) set. This
        holds for ``restart`` too: re-walking an anchored cursor re-covers the
        WINDOW from its first page, not the address's full history — the window is
        what that cursor's pages mean."""
        async for page, items in self._client.address_transactions(
            address,
            order="asc",
            start_page=start_page,
            max_items=max_items,
            from_block=from_block if from_block is not None else anchor,
            to_block=to_block,
        ):
            yield _page_cursor(page, anchor), [it["tx_hash"] for it in items]

    async def _recent_anchor(
        self,
        address: str,
        n: int,
        progress: Callable[[str], None],
        *,
        to_block: str | None = None,
    ) -> str | None:
        """Block height of the address's nth-newest transaction (a cheap
        hashes-only desc walk), or None when the address has fewer than ``n`` txs
        or the provider omits the height — an anchor-less full walk is then the
        correct degradation. With ``to_block`` set the desc walk is bounded
        above, so the anchor names the nth-newest tx AT OR BELOW that height
        (how the history backfill windows itself strictly below the host's
        earliest ingested block)."""
        count = 0
        async for _page, items in self._client.address_transactions(
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

    async def _policy_tx_hashes(self, policy_id: str) -> AsyncIterator[str]:
        """Yield every transaction hash touching any asset under ``policy_id``
        (with duplicates, in discovery order)."""
        async for assets in self._client.policy_assets(policy_id):
            for asset in assets:
                async for txs in self._client.asset_transactions(asset["asset"]):
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
        """Stream the unique tx hashes touching a policy as synthetic fixed-size
        pages, so discovery overlaps ingestion, memory holds only one page (plus the
        unavoidable dedup set), and a daily-limit raised mid-discovery propagates to
        the caller. Resume re-walks from the start (upstream ordering is stable) but
        skips pages before ``start_page`` — only the cheap discovery requests repeat,
        not the expensive tx/utxo fetches. ``max_items`` caps the FULL unique-hash
        target, accounting for the skipped offset via ``start_page``.
        """
        page_size = self._settings.blockfrost_page_size
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
            if len(buf) >= page_size:
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
        # An explicit block range overrides a stored anchor; don't stamp the stale
        # anchor on the yielded cursors (page numbers would name positions in the
        # explicit range's result set, not the anchor's).
        if from_block is not None:
            anchor = None
        start_page = _start_page(page, mode)
        try:
            if address is not None:
                # (Re)anchor a capped walk to the recent window. Two cases anchor:
                #   * a fresh/empty parsed cursor (page, anchor) == (0, None) — a new
                #     onboard, or a rate-limited pre-walk retry that arrives as
                #     mode="resume" with an empty cursor and must re-run the pre-walk;
                #   * a RESTART — re-walking from page 1 for the requested ``max_items``
                #     (e.g. "download more" raising the cap), which must re-anchor LOWER
                #     to widen the window rather than re-cover the frozen one. The engine
                #     pairs this by sizing ``max_items`` to the FULL cap on a recent
                #     restart (see ingester.ingest's ``recent_restart``).
                # NOT keyed on mode alone: a mid-walk RESUME keeps its stored anchor (or
                # stays unfiltered for a legacy "page:N" cursor) — re-anchoring mid-walk
                # would skip data, and filtered page numbers wouldn't line up.
                if (
                    window == "recent"
                    and max_items
                    and from_block is None
                    and mode != "tip"
                    and ((page, anchor) == (0, None) or mode == "restart")
                ):
                    # ``to_block`` composes with the recent window: the anchor is
                    # found inside the bounded set, so the walk covers the N most
                    # recent txs at or below the bound. The bound is re-supplied
                    # per call and only truncates the TOP of the asc walk, so if
                    # it rises between runs, items append at the end of the
                    # filtered set and earlier pages stay identical — an anchored
                    # cursor therefore resumes safely across bound changes.
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
        except BlockfrostNotFoundError as exc:
            raise SourceNotFound(str(exc)) from exc
        except BlockfrostDailyLimitError as exc:
            raise SourceRateLimited(str(exc)) from exc
        except BlockfrostError as exc:
            raise SourceError(str(exc)) from exc

    # --- Per-tx fetch + metadata ----------------------------------------------

    async def fetch_tx(self, target: str, target_type: str, tx_hash: str) -> NormalizedTx:
        try:
            detail = await self._client.tx(tx_hash)
            utxos = await self._client.tx_utxos(tx_hash)
        except BlockfrostNotFoundError as exc:
            raise SourceNotFound(str(exc)) from exc
        except BlockfrostDailyLimitError as exc:
            raise SourceRateLimited(str(exc)) from exc
        except BlockfrostError as exc:
            raise SourceError(str(exc)) from exc
        tx, utxo_rows, asset_rows = build_records(target, target_type, detail, utxos)
        return NormalizedTx(tx, utxo_rows, asset_rows)

    async def metadata(self, target: str, target_type: str) -> TargetMeta:
        try:
            return await fetch_contract_metadata(self._client, target, target_type)
        except BlockfrostNotFoundError as exc:
            raise SourceNotFound(str(exc)) from exc
        except BlockfrostDailyLimitError as exc:
            raise SourceRateLimited(str(exc)) from exc
        except BlockfrostError as exc:
            raise SourceError(str(exc)) from exc
