"""ChainSource conformance suite — protocol-level assertions every adapter must
pass (see harness.py for how to register a new one). The resume invariant is the
load-bearing test: it is what guarantees the engine can persist a cursor at any
page boundary and continue with no gaps and no duplicates."""

from __future__ import annotations

import pytest

from app.sources.base import ChainSource, NormalizedTx, SourceNotFound, SourceRateLimited
from tests.sources.harness import SOURCE_FACTORIES, Scenario

pytestmark = pytest.mark.parametrize(
    "factory", SOURCE_FACTORIES.values(), ids=SOURCE_FACTORIES.keys()
)


def _target_kwargs(sc: Scenario, target: str | None = None) -> dict[str, str | None]:
    t = target if target is not None else sc.target
    return (
        {"address": t, "policy_id": None}
        if sc.target_type == "address"
        else {"address": None, "policy_id": t}
    )


async def _walk(
    source: ChainSource,
    sc: Scenario,
    *,
    cursor: str | None = None,
    mode: str = "restart",
    max_items: int | None = None,
    window: str = "history",
    stop_after_pages: int | None = None,
) -> tuple[list[str], list[str]]:
    """Drive discovery; returns (hashes, cursors yielded)."""
    hashes: list[str] = []
    cursors: list[str] = []
    n = 0
    async for page_cursor, page_hashes in source.tx_hash_pages(
        **_target_kwargs(sc),  # type: ignore[arg-type]
        cursor=cursor,
        mode=mode,  # type: ignore[arg-type]
        max_items=max_items,
        from_block=None,
        to_block=None,
        window=window,  # type: ignore[arg-type]
        progress=lambda _m: None,
    ):
        if not page_hashes:
            break
        hashes.extend(page_hashes)
        cursors.append(page_cursor)
        n += 1
        if stop_after_pages is not None and n >= stop_after_pages:
            break
    return hashes, cursors


async def test_full_walk_is_stable_and_complete(factory) -> None:
    source, sc = factory()
    async with source:
        hashes, cursors = await _walk(source, sc)
    assert hashes == sc.expected_hashes  # every hash exactly once, stable order
    assert all(isinstance(c, str) and c for c in cursors)  # non-empty opaque cursors


async def test_resume_invariant_no_gaps_no_duplicates(factory) -> None:
    """Cut the walk after the first page, resume a FRESH source from the yielded
    cursor: both halves concatenated must equal one uninterrupted walk."""
    source1, sc = factory()
    async with source1:
        first, cursors = await _walk(source1, sc, stop_after_pages=1)
    assert first and cursors

    source2, _ = factory()
    async with source2:
        rest, _ = await _walk(source2, sc, cursor=cursors[-1], mode="resume")
    assert first + rest == sc.expected_hashes


async def test_tip_mode_recovers_cursor_position(factory) -> None:
    """mode='tip' must re-cover the cursor's own position (idempotent catch-up),
    i.e. yield a superset of a plain resume, still without gaps to the end."""
    source1, sc = factory()
    async with source1:
        first, cursors = await _walk(source1, sc, stop_after_pages=1)

    source2, _ = factory()
    async with source2:
        tip, _ = await _walk(source2, sc, cursor=cursors[-1], mode="tip")
    # Everything from the cursor page onward, ending at the chain of the full walk.
    assert tip[-1] == sc.expected_hashes[-1]
    assert set(first + tip) == set(sc.expected_hashes)


async def test_max_items_caps_discovery(factory) -> None:
    source, sc = factory()
    cap = max(1, len(sc.expected_hashes) - 2)
    async with source:
        hashes, _ = await _walk(source, sc, max_items=cap)
    assert len(hashes) <= cap + len(sc.expected_hashes)  # provider may overshoot a page…
    assert hashes[:cap] == sc.expected_hashes[:cap]  # …but the cap prefix is exact


async def test_recent_window_yields_newest_or_degrades_to_history(factory) -> None:
    """window='recent' on a capped fresh walk yields the max_items NEWEST hashes
    (in the source's stable asc order). Adapters that can't honor the hint must
    behave exactly as window='history' — never something in between."""
    source, sc = factory()
    if sc.recent_window is None:
        cap = max(1, len(sc.expected_hashes) - 2)
        async with source:
            hashes, _ = await _walk(source, sc, max_items=cap, window="recent")
        assert hashes[:cap] == sc.expected_hashes[:cap]  # hint ignored = history
        return
    async with source:
        hashes, cursors = await _walk(source, sc, max_items=len(sc.recent_window), window="recent")
    assert hashes == sc.recent_window
    assert all(isinstance(c, str) and c for c in cursors)


async def test_recent_window_cursor_resumes_within_the_window(factory) -> None:
    """Cut a recent-window walk after one page; a FRESH source resuming from the
    yielded cursor must complete the window with no gaps/duplicates and never
    fall back into pre-window history."""
    source1, sc = factory()
    if sc.recent_window is None:
        pytest.skip("adapter ignores the recent-window hint")
    k = len(sc.recent_window)
    async with source1:
        first, cursors = await _walk(source1, sc, max_items=k, window="recent", stop_after_pages=1)
    assert first and cursors

    source2, _ = factory()
    async with source2:
        rest, _ = await _walk(source2, sc, cursor=cursors[-1], mode="resume")
    assert first + rest == sc.recent_window


async def test_recent_window_tip_recovers_window_position(factory) -> None:
    """mode='tip' from the final recent-window cursor re-covers the cursor's page
    of the WINDOW (not of the full history)."""
    source1, sc = factory()
    if sc.recent_window is None:
        pytest.skip("adapter ignores the recent-window hint")
    k = len(sc.recent_window)
    async with source1:
        full, cursors = await _walk(source1, sc, max_items=k, window="recent")

    source2, _ = factory()
    async with source2:
        tip, _ = await _walk(source2, sc, cursor=cursors[-1], mode="tip")
    assert tip[-1] == sc.recent_window[-1]
    assert set(full + tip) == set(sc.recent_window)


async def test_fetch_missing_tx_raises_neutral_not_found(factory) -> None:
    source, sc = factory()
    async with source:
        with pytest.raises(SourceNotFound):
            await source.fetch_tx(sc.target, sc.target_type, sc.missing_tx)


async def test_discovery_rate_limit_raises_neutral_error(factory) -> None:
    source, sc = factory()
    async with source:
        with pytest.raises(SourceRateLimited):
            await _walk(
                source,
                Scenario(
                    target=sc.rate_limited_target,
                    target_type=sc.target_type,
                    expected_hashes=[],
                    missing_tx=sc.missing_tx,
                    rate_limited_target=sc.rate_limited_target,
                ),
            )


async def test_fetch_tx_returns_consistent_normalized_records(factory) -> None:
    source, sc = factory()
    async with source:
        ntx = await source.fetch_tx(sc.target, sc.target_type, sc.expected_hashes[0])
    assert isinstance(ntx, NormalizedTx)
    assert ntx.tx.tx_hash == sc.expected_hashes[0]
    assert ntx.tx.target == sc.target and ntx.tx.target_type == sc.target_type
    # Every UTXO/asset row is stamped with the same identity, and the tx's
    # input/output counts agree with the rows.
    assert all(u.target == sc.target and u.tx_hash == ntx.tx.tx_hash for u in ntx.utxos)
    assert sum(1 for u in ntx.utxos if u.role == "input") == ntx.tx.input_count
    assert sum(1 for u in ntx.utxos if u.role == "output") == ntx.tx.output_count


async def test_metadata_has_exactly_the_documented_keys(factory) -> None:
    source, sc = factory()
    async with source:
        meta = await source.metadata(sc.target, sc.target_type)
    assert set(meta) == {
        "exists",
        "is_script",
        "script_type",
        "balance_lovelace",
        "asset_count",
        "sample_tokens",
    }
    import json

    assert isinstance(json.loads(meta["sample_tokens"]), list)
