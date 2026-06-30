"""Pluggable notification module — the alerting system.

Public surface:
  - ``on_new_scores(results, network)`` — the engine's only entry point. Runs
    on the ClickHouse executor thread; never blocks the scoring loop.
  - ``set_main_loop(loop)`` / ``build_channels()`` / ``load_config()`` —
    lifespan wiring.

Why the work is split across a thread boundary: ``engine.run_once`` (and thus
``on_new_scores``) runs on a thread-pool executor, which cannot ``await``. So
``on_new_scores`` does only fast in-memory work and schedules the async
delivery onto the captured main loop with ``run_coroutine_threadsafe``,
discarding the future — fire-and-forget, so a slow/broken channel can never
stall scoring. The dedup claim + the channel sends run in
``_claim_then_dispatch`` on the main loop.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.db import postgres
from app.notifications import config, dispatcher, registry, triggers
from app.notifications.payloads import build_immediate_alert

logger = logging.getLogger(__name__)

# Captured at startup (main.lifespan) so the executor-thread hook can schedule
# coroutines onto the running event loop. None => notifications are inert.
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Set/clear the captured main event loop (startup / shutdown / tests)."""
    global _main_loop
    _main_loop = loop


async def load_config() -> None:
    """Load + validate the config from the DB into the cache (seeding the safe
    default on first boot), then warn on public webhook egress. Fails the boot
    loudly if the stored document is invalid."""
    await config.refresh_from_db()
    config.warn_if_webhook_egress_public()


def build_channels() -> None:
    """Instantiate the configured delivery channels (startup)."""
    registry.build_channels()


def on_new_scores(results: List[Dict[str, Any]], network: str) -> None:
    """Emit immediate alerts for newly-written scores.

    Called from ``engine.run_once`` on the ClickHouse executor thread, right
    after the scores are durably inserted. Synchronous and fast: it filters,
    resolves channels, builds the payload, and schedules fire-and-forget
    delivery on the main loop. It NEVER awaits, so scoring is never blocked.
    """
    loop = _main_loop
    if loop is None or not loop.is_running():
        return  # notifications not wired (or shutting down) — stay inert
    for r in results:
        try:
            band = r.get("risk_band")
            # Skip non-detections: a benign tx scores max_class="" / max_score 0
            # (band Informational). That is not an alert on ANY channel, even
            # when a band is routed for diagnostics.
            if not band or not r.get("max_class") or float(r.get("max_score") or 0) <= 0:
                continue
            # The trigger matrix (DB-backed config) is the single source of truth
            # for which (band, class) page and to where. Bands with no configured channels
            # (Moderate/Informational by default) resolve to [] and are skipped
            # here before any I/O.
            dispatches = triggers.resolve_dispatch(band, r["max_class"])
            if not dispatches:
                continue
            payload = build_immediate_alert(r, network)
            asyncio.run_coroutine_threadsafe(
                _claim_then_dispatch(network, r["tx_hash"], band, payload, dispatches),
                loop,
            )  # future intentionally discarded — fire-and-forget
        except Exception:
            # One malformed result — or the loop closing mid-shutdown — must
            # never propagate into run_once and stall the scoring watermark.
            logger.exception(
                "notification: skipping result %s", r.get("tx_hash") if isinstance(r, dict) else "?"
            )


async def _claim_then_dispatch(
    network: str, tx_hash: str, band: str, payload, dispatches,
) -> None:
    """On the main loop: de-dup, then fan out. Resolve-then-claim ordering."""
    try:
        claimed = await postgres.claim_notification(network, tx_hash, band)
    except Exception:
        logger.exception("notification dedup claim failed for %s/%s", network, tx_hash)
        return
    if not claimed:
        return  # already notified at >= this band (duplicate / non-escalating)
    await dispatcher.dispatch(payload, dispatches)
