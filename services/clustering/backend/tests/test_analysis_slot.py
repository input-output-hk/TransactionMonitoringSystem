"""The ad-hoc analysis endpoints bound concurrency so a burst of heavy runs
(full-window load + DBSCAN + O(n^2) silhouette) can't overload ClickHouse / the
box. Excess callers WAIT for a slot; they are never rejected."""

from __future__ import annotations

import threading
import time

import pytest

from app.api import deps


def test_analysis_slot_bounds_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "_analysis_semaphore", threading.Semaphore(2))
    lock = threading.Lock()
    current = 0
    peak = 0

    def work() -> None:
        nonlocal current, peak
        with deps.analysis_slot():
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.02)  # hold the slot long enough to overlap
            with lock:
                current -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Never more than the cap ran at once, and all 8 still completed (waited).
    assert peak <= 2
    # The semaphore is fully released afterwards (no leaked permits).
    assert deps._analysis_semaphore.acquire(blocking=False)
    assert deps._analysis_semaphore.acquire(blocking=False)
    assert not deps._analysis_semaphore.acquire(blocking=False)
