"""Orchestrates downloading a target's transactions into ClickHouse.

Source-agnostic: it drives a ``ChainSource`` (host_ch today, a node/db-sync adapter
later) and never sees a provider-specific payload: the source yields tx-hash pages and
already-normalized transactions. Ingestion is:
  * rate-limited   — by the source's own admission control;
  * resumable      — a per-target cursor records the last completed page;
  * boundable      — `max_txs` / block range stop early;
  * graceful on quota — ``SourceRateLimited`` stops the run and persists the cursor.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from app.config import get_settings
from app.models import AssetRecord, TxRecord, UtxoRecord
from app.sources.base import (
    ChainSource,
    DiscoveryMode,
    DiscoveryWindow,
    NormalizedTx,
    SourceNotFound,
    SourceRateLimited,
)
from app.storage.protocol import Repo

ProgressFn = Callable[[str], None]


class TargetKwargs(TypedDict, total=False):
    """Exactly one of these is set — selects how a target is addressed. Lets
    callers build ``{"address": ...}`` / ``{"policy_id": ...}`` and splat it into
    ``ingest(**...)`` without mypy widening it to an arbitrary ``dict[str, str]``."""

    address: str
    policy_id: str


async def _bounded_fetch(
    source: ChainSource, target: str, target_type: str, tx_hash: str, sem: asyncio.Semaphore
) -> NormalizedTx:
    """Fetch + normalize one tx, bounded by ``sem``. The source's own admission
    control still serializes upstream requests, so concurrency overlaps round-trip
    latency without exceeding the configured request rate."""
    async with sem:
        return await source.fetch_tx(target, target_type, tx_hash)


@dataclass(slots=True)
class IngestResult:
    target: str
    target_type: str
    status: str  # 'completed' | 'rate_limited' | 'max_reached'
    txs_ingested: int
    # The source-owned resume cursor as persisted at the end of this run (opaque
    # to the engine; a page-based adapter encodes "page:N", or "page:N;from:H" when
    # the walk is anchored to a recent window, and a node adapter a point).
    cursor: str


def _noop(_: str) -> None:  # pragma: no cover
    pass


@dataclass(slots=True)
class _Batch:
    """Buffers records and flushes them to the repo in a single batched insert."""

    txs: list[TxRecord] = field(default_factory=list)
    utxos: list[UtxoRecord] = field(default_factory=list)
    assets: list[AssetRecord] = field(default_factory=list)

    def add(self, tx: TxRecord, utxo_rows: list[UtxoRecord], asset_rows: list[AssetRecord]) -> None:
        self.txs.append(tx)
        self.utxos.extend(utxo_rows)
        self.assets.extend(asset_rows)

    def __len__(self) -> int:
        return len(self.txs)

    def flush(self, repo: Repo) -> None:
        repo.insert_transactions(self.txs)
        repo.insert_utxos(self.utxos)
        repo.insert_assets(self.assets)
        self.txs.clear()
        self.utxos.clear()
        self.assets.clear()


@dataclass(slots=True)
class _PageDrain:
    """Outcome of folding one page's fetch results into the batch."""

    seen: int
    last_tx_hash: str
    max_reached: bool


def _consume_results(
    page_hashes: list[str],
    results: list[Any],
    *,
    batch: _Batch,
    repo: Repo,
    seen: int,
    max_txs: int | None,
    batch_size: int,
    progress: ProgressFn,
) -> _PageDrain:
    """Fold one page's fetch results into ``batch``: skip not-found txs, re-raise
    unexpected errors, and flush when the batch fills. Returns the updated count,
    the last successfully-processed hash, and whether ``max_txs`` was hit mid-page.

    Each result is a ``NormalizedTx`` (the source already normalized it) or an
    exception captured by ``asyncio.gather(return_exceptions=True)``.
    """
    last_tx_hash = ""
    for tx_hash, r in zip(page_hashes, results, strict=True):
        if isinstance(r, SourceNotFound):
            progress(f"tx {tx_hash[:12]}… not found; skipping.")
            continue
        if isinstance(r, BaseException):
            raise r
        batch.add(r.tx, r.utxos, r.assets)
        seen += 1
        last_tx_hash = tx_hash

        if len(batch) >= batch_size:
            batch.flush(repo)
        if max_txs is not None and seen >= max_txs:
            return _PageDrain(seen, tx_hash, True)
    return _PageDrain(seen, last_tx_hash, False)


@dataclass(slots=True)
class _WalkPlan:
    """Where a discovery walk starts: the (provider-neutral) mode, the stored
    cursor to hand the source, and the txs already seen by prior runs."""

    mode: DiscoveryMode
    stored_cursor: str | None
    seen: int


def _plan_walk(
    repo: Repo, target: str, *, resume: bool, from_tip: bool, progress: ProgressFn
) -> _WalkPlan:
    """Resolve the stored cursor into a walk plan.

    Provider-neutral logic only: what the cursor MEANS is the source's business.
    A cursor written by a different CHAIN_SOURCE is treated as absent (a page-based
    adapter's page number is garbage to a node adapter; restarting is safe because
    inserts are idempotent)."""
    mode: DiscoveryMode
    cursor_row = repo.get_cursor(target) if resume else None
    chain_source = get_settings().chain_source
    if cursor_row and cursor_row.get("source") and cursor_row["source"] != chain_source:
        progress(
            f"ignoring cursor from source {cursor_row['source']!r} "
            f"(current CHAIN_SOURCE is {chain_source!r}); re-walking."
        )
        cursor_row = None

    seen = int(cursor_row["txs_seen"]) if cursor_row else 0
    stored_cursor: str | None = (cursor_row.get("cursor") or None) if cursor_row else None

    if cursor_row is None:
        mode = "restart"
    elif not cursor_row["done"]:
        mode = "resume"
    elif from_tip:
        # Incremental catch-up: the source re-covers the cursor's position (cheap
        # and idempotent under ReplacingMergeTree) so newly-appended txs are found.
        mode = "tip"
        progress(f"{target} fully ingested; checking for new transactions from the tip.")
    else:
        mode = "restart"
        # "Start" is source-defined: an anchored (recent-window) cursor re-walks
        # from the start of its window, an unanchored one from genesis.
        progress(f"{target} already fully ingested; re-walking from the start.")
    return _WalkPlan(mode=mode, stored_cursor=stored_cursor, seen=seen)


@dataclass(slots=True)
class _CursorTracker:
    """Owns the run's cursor persistence: what to save after each page, on an
    interruption, and at completion. The engine never computes cursors — this
    only persists what the source yielded (``completed_cursor``)."""

    repo: Repo
    target: str
    target_type: str
    mode: DiscoveryMode
    stored_cursor: str | None
    seen: int
    completed_cursor: str | None = None

    def save(self, *, cursor: str, done: bool, last_tx_hash: str = "") -> None:
        self.repo.upsert_cursor(
            self.target,
            self.target_type,
            cursor=cursor,
            last_tx_hash=last_tx_hash,
            txs_seen=self.seen,
            done=done,
        )

    def page_done(self, cursor: str, *, seen: int, last_tx_hash: str) -> None:
        """A page's txs are fully ingested: advance and persist (done=False)."""
        self.seen = seen
        self.completed_cursor = cursor
        self.save(cursor=cursor, last_tx_hash=last_tx_hash, done=False)

    def interrupted(self) -> str | None:
        """What to persist when the run stops before finishing: the last page
        completed this run; else (resume) the stored cursor unchanged; else
        (restart) the beginning. A ``tip`` run interrupted before any page
        completed persists NOTHING — the row stays done=True, so the next tip
        run re-covers the same position instead of mis-resuming past it."""
        if self.completed_cursor is not None:
            return self.completed_cursor
        if self.mode == "resume":
            return self.stored_cursor or ""
        if self.mode == "restart":
            return ""
        return None  # tip, nothing completed → leave the stored row alone

    def rate_limited(self, progress: ProgressFn, where: str) -> IngestResult:
        cur = self.interrupted()
        if cur is not None:
            self.save(cursor=cur, done=False)
        progress(f"Rate limit hit {where}; cursor saved. Resume later.")
        return IngestResult(
            self.target,
            self.target_type,
            "rate_limited",
            self.seen,
            cur or self.stored_cursor or "",
        )

    def completed(self, *, final_seen: int) -> IngestResult:
        """The walk ran to the end: persist done=True at the last completed page
        (or the prior state when nothing new was walked)."""
        self.seen = final_seen
        final = (
            self.completed_cursor
            if self.completed_cursor is not None
            else (self.stored_cursor or "")
        )
        self.save(cursor=final, done=True)
        return IngestResult(self.target, self.target_type, "completed", self.seen, final)


async def _drain_pages(
    pages: AsyncIterator[tuple[str, list[str]]],
    *,
    source: ChainSource,
    repo: Repo,
    tracker: _CursorTracker,
    batch: _Batch,
    max_txs: int | None,
    batch_size: int,
    concurrency: int,
    progress: ProgressFn,
) -> IngestResult | None:
    """Walk the discovery pages, ingesting each page's txs concurrently.

    Returns an ``IngestResult`` when the walk stops early (rate-limited mid-page
    or during discovery, or ``max_txs`` reached) and ``None`` when it runs to the
    end — the caller then writes the done=True cursor and builds the completed
    result. Flushes the batch at every exit point it owns; the caller's
    ``finally`` flush is the backstop."""
    target, target_type = tracker.target, tracker.target_type
    try:
        async for page_cursor, hashes in pages:
            # Fetch only what we still need (respect max_txs precisely), concurrently.
            page_hashes = hashes if max_txs is None else hashes[: max(0, max_txs - tracker.seen)]
            if not page_hashes:
                break
            sem = asyncio.Semaphore(concurrency)
            results = await asyncio.gather(
                *(_bounded_fetch(source, target, target_type, h, sem) for h in page_hashes),
                return_exceptions=True,
            )

            # A rate/quota limit anywhere in the page stops the run; the whole page
            # is re-fetched on resume (inserts are idempotent under ReplacingMergeTree).
            if any(isinstance(r, SourceRateLimited) for r in results):
                batch.flush(repo)
                return tracker.rate_limited(progress, "mid-page")

            drained = _consume_results(
                page_hashes,
                results,
                batch=batch,
                repo=repo,
                seen=tracker.seen,
                max_txs=max_txs,
                batch_size=batch_size,
                progress=progress,
            )
            if drained.max_reached:
                batch.flush(repo)
                tracker.seen = drained.seen
                tracker.save(cursor=page_cursor, last_tx_hash=drained.last_tx_hash, done=False)
                progress(f"Reached max_txs={max_txs}.")
                return IngestResult(target, target_type, "max_reached", drained.seen, page_cursor)

            batch.flush(repo)
            tracker.page_done(page_cursor, seen=drained.seen, last_tx_hash=drained.last_tx_hash)
            progress(f"{page_cursor} done — {drained.seen} transactions ingested so far.")
    except SourceRateLimited:
        # A quota limit raised while *discovering* pages (the address/policy tx-hash
        # listing), as opposed to during a per-tx fetch (handled in-loop above).
        batch.flush(repo)
        return tracker.rate_limited(progress, "during discovery")
    return None


def _resolve_window(
    *, address: str | None, recent: bool, max_txs: int | None, progress: ProgressFn
) -> DiscoveryWindow:
    """Pick the discovery window. Only an *address* target with a cap can window to
    ``recent`` (newest-first); a policy target discovers via its assets and can't
    window, so it falls back to ``history`` with a note."""
    if not (recent and max_txs):
        return "history"
    if address is not None:
        return "recent"
    progress(
        "policy targets discover via assets and can't window to recent "
        "transactions; ingesting from history."
    )
    return "history"


def _discovery_max_items(
    *,
    address: str | None,
    max_txs: int | None,
    remaining: int | None,
    window: DiscoveryWindow,
    mode: DiscoveryMode,
) -> int | None:
    """How many items the source should size discovery to (its ``max_items``).

    Address pagination normally resumes forward, so it only needs the REMAINING
    count. But a recent-window RESTART re-walks the window from its start (re-
    covering already-ingested txs idempotently), so it must size discovery to the
    FULL N — otherwise raising the cap ("Download more") re-anchors but can't reach
    the older history the wider window now spans. Policy discovery always re-walks
    from the start and skips already-done pages, so it caps on the FULL target.

    INVARIANT (paired with a recent-window-anchoring source's tx_hash_pages): on a
    recent restart the source re-anchors to ``max_items``, so this MUST pass the
    full ``max_txs``; passing ``remaining`` would anchor at too-small an N and
    under-fetch.
    """
    if address is None:
        return max_txs
    recent_restart = window == "recent" and mode == "restart"
    return max_txs if recent_restart else remaining


async def ingest(
    *,
    repo: Repo,
    source: ChainSource,
    address: str | None = None,
    policy_id: str | None = None,
    max_txs: int | None = None,
    from_block: str | None = None,
    to_block: str | None = None,
    recent: bool = False,
    resume: bool = True,
    from_tip: bool = False,
    batch_size: int | None = None,
    concurrency: int | None = None,
    progress: ProgressFn = _noop,
) -> IngestResult:
    if (address is None) == (policy_id is None):
        raise ValueError("Provide exactly one of address or policy_id.")
    if recent and (from_block is not None or to_block is not None):
        raise ValueError("recent is mutually exclusive with from_block/to_block.")

    if batch_size is None:
        batch_size = get_settings().ingest_batch_size

    target = address or policy_id
    assert target is not None
    target_type = "address" if address else "policy"

    window = _resolve_window(address=address, recent=recent, max_txs=max_txs, progress=progress)

    plan = _plan_walk(repo, target, resume=resume, from_tip=from_tip, progress=progress)

    remaining = None if max_txs is None else max(0, max_txs - plan.seen)
    if remaining == 0:
        progress(f"Already at max_txs={max_txs} for {target}; nothing to do.")
        return IngestResult(target, target_type, "max_reached", plan.seen, plan.stored_cursor or "")

    batch = _Batch()
    tracker = _CursorTracker(
        repo=repo,
        target=target,
        target_type=target_type,
        mode=plan.mode,
        stored_cursor=plan.stored_cursor,
        seen=plan.seen,
    )

    page_max_items = _discovery_max_items(
        address=address,
        max_txs=max_txs,
        remaining=remaining,
        window=window,
        mode=plan.mode,
    )
    pages = source.tx_hash_pages(
        address=address,
        policy_id=policy_id,
        cursor=plan.stored_cursor,
        mode=plan.mode,
        max_items=page_max_items,
        from_block=from_block,
        to_block=to_block,
        window=window,
        progress=progress,
    )

    concurrency = max(
        1, concurrency if concurrency is not None else get_settings().ingest_concurrency
    )

    try:
        early = await _drain_pages(
            pages,
            source=source,
            repo=repo,
            tracker=tracker,
            batch=batch,
            max_txs=max_txs,
            batch_size=batch_size,
            concurrency=concurrency,
            progress=progress,
        )
    finally:
        batch.flush(repo)
    if early is not None:
        return early

    # A tip run re-fetches the cursor's (already-counted) position, so the running
    # `seen` double-counts those rows. Persist the true distinct count instead, so
    # the cursor's txs_seen stays accurate for any later bounded onboarding/backfill.
    final_seen = repo.count_transactions(target) if from_tip else tracker.seen
    return tracker.completed(final_seen=final_seen)
