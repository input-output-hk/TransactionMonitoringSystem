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
stall scoring. The delivery and the dedup claim run in
``_deliver_with_dedup`` on the main loop.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.config import settings
from app.db import postgres
from app.notifications import config, dispatcher, registry, triggers
from app.notifications.payloads import build_immediate_alert

logger = logging.getLogger(__name__)

# Outcome of a dedup-gated delivery, returned by _deliver_with_dedup so a caller
# that budgets send attempts (the contract_anomaly poller) can tell a free
# no-op from a real attempt. DUPLICATE cost nothing on the wire; SENT and FAILED
# both consumed a delivery attempt (FAILED still hit the channel, e.g. a timeout
# or SMTP error), so both count against a per-tick cap. FAILED additionally
# signals "retry me" (no claim was recorded), which the poller uses to keep
# draining rather than idle on an unchanged upstream.
DELIVER_DUPLICATE = "duplicate"
DELIVER_SENT = "sent"
DELIVER_FAILED = "failed"

# Captured at startup (main.lifespan) so the executor-thread hook can schedule
# coroutines onto the running event loop. None => notifications are inert.
_main_loop: Optional[asyncio.AbstractEventLoop] = None

# Bounds concurrent deliveries (see NOTIFY_MAX_CONCURRENT_DELIVERIES). Created
# lazily on the main loop (asyncio.Semaphore binds to the running loop) and
# reset with the loop so a fresh loop / test gets its own.
_delivery_sema: Optional[asyncio.Semaphore] = None


def set_main_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Set/clear the captured main event loop (startup / shutdown / tests)."""
    global _main_loop, _delivery_sema
    _main_loop = loop
    _delivery_sema = None  # recreated on the new loop on first delivery


def _get_delivery_sema() -> asyncio.Semaphore:
    """The shared delivery concurrency limiter, created lazily on the loop."""
    global _delivery_sema
    if _delivery_sema is None:
        _delivery_sema = asyncio.Semaphore(settings.NOTIFY_MAX_CONCURRENT_DELIVERIES)
    return _delivery_sema


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
                _deliver_with_dedup(network, r["tx_hash"], band, payload, dispatches),
                loop,
            )  # future intentionally discarded — fire-and-forget
        except Exception:
            # One malformed result — or the loop closing mid-shutdown — must
            # never propagate into run_once and stall the scoring watermark.
            logger.exception(
                "notification: skipping result %s", r.get("tx_hash") if isinstance(r, dict) else "?"
            )


async def _deliver_with_dedup(
    network: str, tx_hash: str, band: str, payload, dispatches,
    source: str = "scorer",
) -> str:
    """On the main loop: skip duplicates, deliver, then record the claim.

    Deliver-then-claim ordering: the dedup is a READ pre-check and the claim is
    written only AFTER at least one channel actually delivered. So a transient
    total-channel failure records nothing and the alert is retried on the next
    re-score (recall-first: never silently drop a real alert). The small TOCTOU
    window where two concurrent re-scores both deliver risks at most a duplicate
    push, never a miss, which is the trade this system prefers.

    ``source`` selects the dedup stream: ``'scorer'`` for the per-tx immediate
    alerts (default) and ``'contract_anomaly'`` for the clustering poller, so
    the two never suppress each other for the same tx.

    Returns one of :data:`DELIVER_DUPLICATE` / :data:`DELIVER_SENT` /
    :data:`DELIVER_FAILED` so a budgeting caller can distinguish a free dedup
    no-op from a real send attempt. The scorer path ignores the return
    (fire-and-forget).
    """
    try:
        if await postgres.already_notified(network, tx_hash, band, source=source):
            return DELIVER_DUPLICATE  # already notified at >= this band
    except Exception:
        # Dedup check failed: prefer a possible duplicate over a missed alert.
        logger.exception(
            "notification dedup check failed for %s/%s; delivering anyway",
            network, tx_hash,
        )
    # Bound concurrent sends so a burst (backlog drain / spam wave) cannot open
    # hundreds of simultaneous SMTP/webhook connections and trip the endpoint's
    # rate limits. Recall-safe: every alert still delivers, just paced.
    async with _get_delivery_sema():
        delivered = await dispatcher.dispatch(payload, dispatches)
    if not delivered:
        return DELIVER_FAILED  # nothing sent: leave unclaimed so a re-score retries
    try:
        await postgres.claim_notification(network, tx_hash, band, source=source)
    except Exception:
        # Delivered but couldn't record the claim: a later re-score may
        # re-notify (duplicate). Acceptable under recall-first.
        logger.exception(
            "notification claim record failed for %s/%s", network, tx_hash,
        )
    return DELIVER_SENT
