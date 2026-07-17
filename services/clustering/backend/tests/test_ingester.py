"""Characterization tests for the ingestion orchestration.

Drives ``ingest()`` against an in-memory fake repo and the in-memory reference
``ChainSource`` (``tests.sources.inmemory``), so the loop / cursor / batching
behaviour is pinned without ClickHouse or the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import get_settings
from app.ingest.ingester import (
    _discovery_max_items,
    _noop,
    _resolve_window,
    ingest,
)
from tests.fakes import FakeRepoBase
from tests.sources.inmemory import InMemoryChainSource

TX_HASHES = ["aa", "bb", "cc"]


class FakeRepo(FakeRepoBase):
    """Records inserts and cursor writes; mimics the methods ingest() uses."""

    def __init__(self) -> None:
        self.txs: list[Any] = []
        self.utxos: list[Any] = []
        self.assets: list[Any] = []
        self.cursors: list[dict[str, Any]] = []
        self._cursor: dict[str, Any] | None = None

    def get_cursor(self, target: str) -> dict[str, Any] | None:
        return self._cursor

    def insert_transactions(self, rows: list[Any]) -> None:
        self.txs.extend(rows)

    def insert_utxos(self, rows: list[Any]) -> None:
        self.utxos.extend(rows)

    def insert_assets(self, rows: list[Any]) -> None:
        self.assets.extend(rows)

    def count_transactions(self, target: str) -> int:
        return len({t.tx_hash for t in self.txs})

    def upsert_cursor(
        self,
        target: str,
        target_type: str,
        *,
        cursor: str,
        last_tx_hash: str,
        txs_seen: int,
        done: bool,
        source: str = "",
    ) -> None:
        self.cursors.append(
            {
                "cursor": cursor,
                "last_tx_hash": last_tx_hash,
                "txs_seen": txs_seen,
                "done": done,
                "source": source,
            }
        )


def _cursor_row(
    target: str,
    cursor: str,
    *,
    txs_seen: int,
    done: int,
    target_type: str = "address",
    last_tx_hash: str = "",
    source: str | None = None,
) -> dict[str, Any]:
    """A stored ingest_cursor row, as get_cursor() returns it."""
    return {
        "target": target,
        "target_type": target_type,
        "cursor": cursor,
        # Default to the active source so the cursor isn't treated as foreign
        # and re-walked (see ingester._plan_walk's source-name check).
        "source": get_settings().chain_source if source is None else source,
        "last_tx_hash": last_tx_hash,
        "txs_seen": txs_seen,
        "done": done,
    }


# Each address's full history as (tx_hash, block_height), ascending — the
# in-memory source pages over this exactly as a real listing endpoint would.
# addr1desc402 hits the daily limit on any DESC listing (pins the recent-window
# pre-walk's 402); addr1midlimit's last tx (limit402) hits it on the per-tx fetch.
ADDRESS_TXS: dict[str, list[tuple[str, int]]] = {
    "addr1demo": [("aa", 10), ("bb", 20), ("cc", 30)],
    "addr1midlimit": [("aa", 10), ("bb", 20), ("limit402", 30)],
    "addr1recent": [("g1", 10), ("g2", 20), ("g3", 30), ("n1", 40), ("n2", 50)],
    "addr1desc402": [("aa", 10), ("bb", 20), ("cc", 30)],
}

# Policy discovery walks a policy's assets, then each asset's transactions; bb
# overlaps the two assets to exercise dedup. pol402's second asset's listing hits
# the daily limit mid-discovery.
POLICY_ASSETS: dict[str, list[str]] = {
    "pol1": ["asset1", "asset2"],
    "pol402": ["asset1", "asset2limit"],
}
ASSET_TXS: dict[str, list[str]] = {"asset1": ["aa", "bb"], "asset2": ["bb", "cc"]}


def _source(*, page_size: int = 2) -> InMemoryChainSource:
    """The in-memory reference source preloaded with the fixtures above, so the
    characterization tests drive ingest() exactly as a real adapter would."""
    return InMemoryChainSource(
        address_txs=ADDRESS_TXS,
        policy_assets=POLICY_ASSETS,
        asset_txs=ASSET_TXS,
        page_size=page_size,
        rate_limited_desc=frozenset({"addr1desc402"}),
        rate_limited_listings=frozenset({"asset2limit"}),
        rate_limited_txs=frozenset({"limit402"}),
    )


async def test_full_address_ingest() -> None:
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1demo")

    assert result.status == "completed"
    assert result.txs_ingested == 3
    assert result.cursor == "page:2"
    assert [t.tx_hash for t in repo.txs] == TX_HASHES
    assert len(repo.utxos) == 6  # 1 input + 1 output per tx
    # Final cursor marks completion with the full count.
    assert repo.cursors[-1]["done"] is True
    assert repo.cursors[-1]["txs_seen"] == 3


async def test_max_txs_stops_early() -> None:
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1demo", max_txs=2)

    assert result.status == "max_reached"
    assert result.txs_ingested == 2
    assert result.cursor == "page:1"
    assert len(repo.txs) == 2
    assert repo.cursors[-1]["done"] is False
    assert repo.cursors[-1]["txs_seen"] == 2


async def test_resume_from_cursor_skips_done_pages() -> None:
    repo = FakeRepo()
    repo._cursor = _cursor_row("addr1demo", "page:1", txs_seen=2, done=0, last_tx_hash="bb")
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1demo")

    # Resumes at page 2, ingesting only the remaining transaction.
    assert result.status == "completed"
    assert [t.tx_hash for t in repo.txs] == ["cc"]
    assert result.txs_ingested == 3


async def test_from_tip_continues_past_done_cursor() -> None:
    # A fully-ingested address (done cursor at the last page). from_tip resumes at
    # that page rather than re-walking from page 1, picking up only new tail txs.
    repo = FakeRepo()
    repo._cursor = _cursor_row("addr1demo", "page:2", txs_seen=3, done=1, last_tx_hash="cc")
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1demo", from_tip=True)

    # Re-fetches page 2 ([cc]) then page 3 (empty) — does NOT re-ingest page 1.
    assert result.status == "completed"
    assert [t.tx_hash for t in repo.txs] == ["cc"]
    # txs_seen is recomputed from the true distinct count, not the inflated running
    # tally (the re-fetched page would otherwise double-count already-seen rows).
    assert repo.cursors[-1]["txs_seen"] == 1


async def test_done_cursor_without_from_tip_restarts_from_page_one() -> None:
    repo = FakeRepo()
    repo._cursor = _cursor_row("addr1demo", "page:2", txs_seen=3, done=1, last_tx_hash="cc")
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1demo")

    # Default behaviour re-walks the whole history from page 1.
    assert result.status == "completed"
    assert [t.tx_hash for t in repo.txs] == ["aa", "bb", "cc"]


async def test_full_policy_ingest() -> None:
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(repo=repo, source=source, policy_id="pol1")

    assert result.status == "completed"
    assert result.txs_ingested == 3  # aa, bb, cc — bb deduped across the two assets
    assert sorted(t.tx_hash for t in repo.txs) == ["aa", "bb", "cc"]
    assert repo.cursors[-1]["done"] is True


async def test_policy_max_txs_stops_early() -> None:
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(repo=repo, source=source, policy_id="pol1", max_txs=2)

    assert result.status == "max_reached"
    assert result.txs_ingested == 2
    assert len(repo.txs) == 2


async def test_policy_resume_skips_done_pages() -> None:
    repo = FakeRepo()
    repo._cursor = _cursor_row(
        "pol1", "page:1", txs_seen=2, done=0, target_type="policy", last_tx_hash="bb"
    )
    async with _source() as source:
        result = await ingest(repo=repo, source=source, policy_id="pol1")

    # Discovery re-walks but page 1 is skipped (not re-ingested); only cc ingests.
    assert [t.tx_hash for t in repo.txs] == ["cc"]
    assert result.txs_ingested == 3


async def test_policy_402_during_discovery_saves_cursor() -> None:
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(repo=repo, source=source, policy_id="pol402")

    # Page 1 (asset1's aa, bb) ingests; the daily limit on asset2's listing then
    # stops the run gracefully with the cursor saved for resume.
    assert result.status == "rate_limited"
    assert [t.tx_hash for t in repo.txs] == ["aa", "bb"]
    assert result.cursor == "page:1"
    assert repo.cursors[-1]["done"] is False


async def test_mid_page_rate_limit_saves_last_completed_cursor() -> None:
    """A 402 during a per-tx FETCH (page 2's tx), as opposed to during discovery:
    the run stops with the cursor at the last fully-ingested page, done=False, so
    a resume re-fetches the interrupted page (idempotent under ReplacingMergeTree).
    Pins _drain_pages' mid-page branch, which the conformance suite can't reach."""
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1midlimit")

    assert result.status == "rate_limited"
    assert result.cursor == "page:1"
    assert [t.tx_hash for t in repo.txs] == ["aa", "bb"]  # page 1 ingested, page 2 not
    last = repo.cursors[-1]
    assert (last["cursor"], last["txs_seen"], last["done"]) == ("page:1", 2, False)


# --- Recent-window capped onboarding (address targets) --------------------------


async def test_recent_capped_ingest_takes_newest_and_anchors_cursor() -> None:
    """max_txs=N with recent=True ingests the N newest txs (asc within the window)
    and persists the from-block anchor inside the cursor, so later walks page
    through the same filtered set instead of the unfiltered history."""
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(
            repo=repo, source=source, address="addr1recent", max_txs=2, recent=True
        )

    assert result.status == "max_reached"
    assert [t.tx_hash for t in repo.txs] == ["n1", "n2"]  # newest 2, not g1/g2
    assert result.cursor == "page:1;from:40"
    assert repo.cursors[-1]["done"] is False


async def test_anchored_cursor_catchup_is_bounded() -> None:
    """The first classify after a recent capped onboard resumes WITHIN the anchored
    window and reaches done=True without ever touching the genesis-era history."""
    repo = FakeRepo()
    repo._cursor = _cursor_row(
        "addr1recent", "page:1;from:40", txs_seen=2, done=0, last_tx_hash="n2"
    )
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1recent", from_tip=True)

    assert result.status == "completed"
    assert repo.txs == []  # nothing new past the window; g1..g3 never ingested
    assert repo.cursors[-1]["done"] is True


async def test_recent_restart_with_raised_cap_widens_the_window() -> None:
    """ "Download more": re-onboarding a fully-ingested, anchored capped contract with a
    larger max_txs must re-anchor LOWER to widen the recent window and pull the older
    txs it now spans — not re-walk the frozen window and ingest nothing. Here the prior
    onboard captured the 2 newest (anchor block 40); raising the cap to 4 re-anchors at
    the 4th-newest (g2 @ block 20) and ingests the 2 older txs (g2, g3)."""
    repo = FakeRepo()
    repo._cursor = _cursor_row(
        "addr1recent", "page:1;from:40", txs_seen=2, done=1, last_tx_hash="n2"
    )
    async with _source() as source:
        result = await ingest(
            repo=repo, source=source, address="addr1recent", max_txs=4, recent=True
        )

    assert result.status == "max_reached"
    assert [t.tx_hash for t in repo.txs] == ["g2", "g3"]  # the older txs the window now covers
    assert repo.cursors[-1]["cursor"] == "page:1;from:20"  # re-anchored lower


async def test_tip_mode_keeps_the_anchor() -> None:
    repo = FakeRepo()
    repo._cursor = _cursor_row(
        "addr1recent", "page:1;from:40", txs_seen=2, done=1, last_tx_hash="n2"
    )
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1recent", from_tip=True)

    # Tip re-covers the anchored page (idempotent) and persists the anchor again.
    assert result.status == "completed"
    assert [t.tx_hash for t in repo.txs] == ["n1", "n2"]
    assert repo.cursors[-1]["cursor"] == "page:1;from:40"
    assert repo.cursors[-1]["done"] is True


async def test_recent_with_fewer_txs_than_cap_degrades_to_full_walk() -> None:
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(
            repo=repo, source=source, address="addr1demo", max_txs=10, recent=True
        )

    assert result.status == "completed"
    assert [t.tx_hash for t in repo.txs] == ["aa", "bb", "cc"]
    assert result.cursor == "page:2"  # anchor-less legacy form


async def test_recent_on_policy_ingests_history_with_note() -> None:
    repo = FakeRepo()
    notes: list[str] = []
    async with _source() as source:
        result = await ingest(
            repo=repo,
            source=source,
            policy_id="pol1",
            max_txs=2,
            recent=True,
            progress=notes.append,
        )

    assert result.status == "max_reached"
    assert len(repo.txs) == 2  # unchanged first-N behaviour
    assert any("can't window to recent" in n for n in notes)


async def test_recent_rejects_explicit_block_range() -> None:
    repo = FakeRepo()
    async with _source() as source:
        with pytest.raises(ValueError, match="mutually exclusive"):
            await ingest(
                repo=repo,
                source=source,
                address="addr1demo",
                max_txs=2,
                recent=True,
                from_block="10",
            )


async def test_rate_limit_during_recent_prewalk_retries_the_prewalk() -> None:
    """A 402 during the desc pre-walk persists an empty cursor (done=False). The
    retry arrives as mode="resume" with that empty cursor and must RE-RUN the
    pre-walk — never fall back to an unfiltered walk from genesis."""
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(
            repo=repo, source=source, address="addr1desc402", max_txs=2, recent=True
        )
    assert result.status == "rate_limited"
    assert repo.txs == []
    assert (repo.cursors[-1]["cursor"], repo.cursors[-1]["done"]) == ("", False)

    repo._cursor = _cursor_row("addr1desc402", "", txs_seen=0, done=0)
    async with _source() as source:
        retry = await ingest(
            repo=repo, source=source, address="addr1desc402", max_txs=2, recent=True
        )

    # Still rate-limited on the desc listing — proving the pre-walk re-ran. (A
    # genesis fallback would have ingested aa/bb from the asc pages instead.)
    assert retry.status == "rate_limited"
    assert repo.txs == []


async def test_explicit_from_block_overrides_and_drops_a_stored_anchor() -> None:
    """An explicit block range wins over a stored anchor, and the yielded cursors
    must NOT carry the stale anchor — its page numbers would name positions in a
    different (anchor-filtered) result set."""
    repo = FakeRepo()
    repo._cursor = _cursor_row(
        "addr1recent", "page:1;from:40", txs_seen=2, done=0, last_tx_hash="n2"
    )
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1recent", from_block="30")

    # from=30 → [g3, n1, n2]; resume starts at page 2 of THAT set → [n2].
    assert result.status == "completed"
    assert [t.tx_hash for t in repo.txs] == ["n2"]
    assert all(";from:" not in c["cursor"] for c in repo.cursors)


async def test_legacy_midwalk_cursor_ignores_recent_hint() -> None:
    """A capped onboard started before the recent-window change left an anchor-less
    mid-walk cursor; re-anchoring mid-walk would skip data, so the walk must
    continue unfiltered and keep producing anchor-less cursors."""
    repo = FakeRepo()
    repo._cursor = _cursor_row("addr1demo", "page:1", txs_seen=2, done=0, last_tx_hash="bb")
    async with _source() as source:
        result = await ingest(repo=repo, source=source, address="addr1demo", max_txs=3, recent=True)

    assert [t.tx_hash for t in repo.txs] == ["cc"]
    assert result.cursor == "page:2"  # still anchor-less
    assert repo.cursors[-1]["cursor"] == "page:2"


# --- Unit tests for the discovery-input helpers ---------------------------------


def test_resolve_window_address_with_cap_is_recent() -> None:
    notes: list[str] = []
    assert (
        _resolve_window(address="addr1demo", recent=True, max_txs=5, progress=notes.append)
        == "recent"
    )
    assert notes == []


def test_resolve_window_without_recent_or_cap_is_history() -> None:
    assert (
        _resolve_window(address="addr1demo", recent=False, max_txs=5, progress=_noop) == "history"
    )
    # recent but uncapped (max_txs falsy) can't window either — both 0 and None.
    assert _resolve_window(address="addr1demo", recent=True, max_txs=0, progress=_noop) == "history"
    assert (
        _resolve_window(address="addr1demo", recent=True, max_txs=None, progress=_noop) == "history"
    )


def test_resolve_window_policy_falls_back_to_history_with_note() -> None:
    notes: list[str] = []
    assert _resolve_window(address=None, recent=True, max_txs=5, progress=notes.append) == "history"
    assert any("can't window to recent" in n for n in notes)


def test_discovery_max_items_policy_caps_on_full_target() -> None:
    # Policy always re-walks from the start, so it sizes to the full cap (or None).
    assert (
        _discovery_max_items(
            address=None, max_txs=50, remaining=10, window="history", mode="restart"
        )
        == 50
    )
    assert (
        _discovery_max_items(
            address=None, max_txs=None, remaining=None, window="history", mode="restart"
        )
        is None
    )


def test_discovery_max_items_recent_restart_passes_full_cap() -> None:
    # The load-bearing invariant: a recent restart must re-anchor to the FULL cap,
    # not the remaining count, or the widened window under-fetches older history.
    assert (
        _discovery_max_items(
            address="addr1demo", max_txs=50, remaining=10, window="recent", mode="restart"
        )
        == 50
    )


def test_discovery_max_items_address_resume_uses_remaining() -> None:
    # Forward resume (and any non-recent / non-restart address walk) only needs what's left.
    assert (
        _discovery_max_items(
            address="addr1demo", max_txs=50, remaining=10, window="history", mode="resume"
        )
        == 10
    )
    assert (
        _discovery_max_items(
            address="addr1demo", max_txs=50, remaining=10, window="recent", mode="resume"
        )
        == 10
    )


class _NamedSource(InMemoryChainSource):
    """The in-memory source posing as a Blockfrost adapter: pins that the cursor
    tag comes from the SOURCE's name, not the global CHAIN_SOURCE (the secondary
    history source runs Blockfrost under a host_ch primary)."""

    name = "blockfrost"


def _named_source(*, page_size: int = 2) -> _NamedSource:
    return _NamedSource(
        address_txs=ADDRESS_TXS,
        policy_assets=POLICY_ASSETS,
        asset_txs=ASSET_TXS,
        page_size=page_size,
    )


async def test_cursor_tagged_with_source_name() -> None:
    repo = FakeRepo()
    async with _named_source() as source:
        result = await ingest(repo=repo, source=source, address="addr1demo")
    assert result.status == "completed"
    assert repo.cursors, "expected cursor writes"
    assert all(c["source"] == "blockfrost" for c in repo.cursors)


async def test_unnamed_source_cursor_falls_back_to_chain_source() -> None:
    # A bare source without a name keeps today's behavior: the cursor is tagged
    # with the primary CHAIN_SOURCE.
    repo = FakeRepo()
    async with _source() as source:
        await ingest(repo=repo, source=source, address="addr1demo")
    assert repo.cursors
    assert all(c["source"] == get_settings().chain_source for c in repo.cursors)


async def test_foreign_cursor_ignored_by_source_name() -> None:
    # A cursor written by a different source is garbage to this one: the walk
    # must restart from the beginning instead of resuming, and re-ingest all txs.
    repo = FakeRepo()
    repo._cursor = _cursor_row("addr1demo", "page:1", txs_seen=2, done=0, source="host_ch")
    async with _named_source() as source:
        result = await ingest(repo=repo, source=source, address="addr1demo")
    assert result.status == "completed"
    assert {t.tx_hash for t in repo.txs} == set(TX_HASHES)
    assert repo.cursors[-1]["source"] == "blockfrost"


async def test_recent_with_to_block_allowed() -> None:
    # recent + to_block = "the most recent N txs at or below the bound" (the
    # history backfill's shape). addr1recent heights: g1:10 g2:20 g3:30 n1:40
    # n2:50; bound 30 + recent 2 anchors at 20 and ingests exactly [g2, g3].
    repo = FakeRepo()
    async with _source() as source:
        result = await ingest(
            repo=repo, source=source, address="addr1recent", max_txs=2, recent=True, to_block="30"
        )
    assert result.status in ("completed", "max_reached")
    assert [t.tx_hash for t in repo.txs] == ["g2", "g3"]


async def test_recent_with_from_block_still_rejected() -> None:
    # from_block fights the recent window's own anchor for the lower bound.
    repo = FakeRepo()
    async with _source() as source:
        with pytest.raises(ValueError, match="mutually exclusive"):
            await ingest(
                repo=repo,
                source=source,
                address="addr1recent",
                max_txs=2,
                recent=True,
                from_block="10",
            )
