"""Background notification tasks: the periodic report scheduler and the
clustering-sidecar contract_anomaly poller.

The report scheduler wakes on a short interval, checks whether a report is due
(the configured frequency vs the persisted ``last_sent_at``), and when due
assembles + dispatches the report, then advances the boundary so a restart
neither double-sends nor skips a period.

The contract_anomaly poller (only started when ``CLUSTERING_ENABLED``) reads the
clustering sidecar's verdicts and fires an immediate alert for each routed one.
contract_anomaly is the sidecar's read-time-only class — it never reaches
``on_new_scores`` — so this poller is its only notification path.

Mirrors :mod:`app.tasks.analysis`: module-level tasks, idempotent
``start()``/``stop()``, and ``_loop``s with per-tick error isolation.
"""

import asyncio
import gzip
import logging
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.db import postgres
from app.notifications import (
    DELIVER_DUPLICATE,
    _deliver_with_dedup,
    config,
    dispatcher,
    reports,
    triggers,
)
from app.notifications.channels.base import Attachment
from app.notifications.payloads import (
    build_contract_anomaly_alert,
    build_degraded_contract_anomaly_alert,
)

logger = logging.getLogger(__name__)

# Above this raw-CSV size, gzip the attachment so base64-encoded email stays
# under common SMTP limits; smaller reports ship as plain .csv (manual-export
# parity).
_CSV_GZIP_THRESHOLD_BYTES = 1_000_000

_task: "asyncio.Task | None" = None
_ca_task: "asyncio.Task | None" = None


async def _tick() -> None:
    if not config.report_enabled():
        return  # disabled — stay idle. Re-enabling via the admin UI hot-refreshes
        #         the cache, so the next tick picks it up without a restart.

    cfg = config.periodic_report_config()
    network = settings.CARDANO_NETWORK
    now = datetime.now(UTC)

    interval = reports.report_interval(cfg["frequency"])
    state = await postgres.get_report_state(network)
    last_sent = state.get("last_sent_at") if state else None
    if last_sent is not None and now < last_sent + interval:
        return  # not due yet

    dispatches = reports.report_dispatches(cfg)
    if not dispatches:
        # Enabled + due but nowhere to deliver (misconfig). Don't build the
        # report or advance the boundary — cheap re-check next tick, and it
        # fires as soon as a channel/recipient is configured + restarted.
        logger.warning("periodic report due but no enabled channel has a recipient/URL; skipping")
        return

    window_days = reports.effective_window_days(cfg["frequency"], cfg["window_days"])
    window_start = now - timedelta(days=window_days)
    report = await reports.build_periodic_report(network, window_start, now, cfg)
    # Attach the same per-transaction CSV the web interface exports, so the
    # report matches the manual download. Email carries it; the webhook gets
    # the JSON payload and ignores attachments.
    csv_bytes = await reports.build_report_csv(network, window_start, now, cfg)
    stamp = now.strftime("%Y%m%d")
    if len(csv_bytes) > _CSV_GZIP_THRESHOLD_BYTES:
        content = gzip.compress(csv_bytes)
        fname, mime = f"tms-report-{network}-{stamp}.csv.gz", "application/gzip"
    else:
        content, fname, mime = csv_bytes, f"tms-report-{network}-{stamp}.csv", "text/csv"
    attachments = [Attachment(filename=fname, content=content, mimetype=mime)]
    await dispatcher.dispatch(report, dispatches, attachments=attachments)
    # Advance only after a completed send attempt (best-effort, like the rest
    # of the outbound path).
    await postgres.mark_report_sent(network, window_start, now, now)
    logger.info(
        "Periodic report sent (window=%dd, channels=%s)",
        window_days,
        [d.channel for d in dispatches],
    )


async def _loop() -> None:
    logger.info(
        "Periodic report scheduler started (check interval=%ss)",
        settings.NOTIFY_REPORT_CHECK_INTERVAL_SECONDS,
    )
    while True:
        try:
            await _tick()
        except Exception:
            # exc_info (via logger.exception) so a bug is diagnosable, not just a
            # repr; the loop keeps running so one bad tick never kills the schedule.
            logger.exception("Periodic report scheduler error")
        await asyncio.sleep(settings.NOTIFY_REPORT_CHECK_INTERVAL_SECONDS)


async def _contract_anomaly_tick() -> None:
    """Poll the clustering sidecar for positive contract_anomaly verdicts and
    fire an immediate alert for each routed one, deduped in the
    ``'contract_anomaly'`` stream so it never collides with the per-tx scorer
    alerts for the same transaction.

    Every tick re-reads the full flagged set and re-evaluates it against the
    (hot-refreshed) trigger config, so a routing change takes effect on the next
    tick and an unclaimed finding (a prior send that failed) is retried without
    any cursor to get wrong. Dedup keeps the output exactly-once, so the re-scan
    is cheap on the wire even though it is O(flagged) on the DB.

    Bounded and observable:
      - the sidecar reads RAISE (a best-effort ``{}`` would let this, the class's
        ONLY alert path, go silently dark on a misconfig/outage: a missed real
        attack with zero observability);
      - per-tick send attempts are capped so a first-enablement backlog drains
        across ticks instead of flooding the channels;
      - a per-tx failure is isolated AND, when the verdict projected to a routed
        band, still delivered as a degraded minimal alert so a build error can
        never silently drop a real finding (recall-first).

    contract_anomaly is read-time-only (never reaches ``on_new_scores``), so this
    poller is its sole notification path.
    """
    from app.analysis import contract_anomaly as ca  # local: keep import tree light
    from app.db import clustering_queries

    network = settings.CARDANO_NETWORK
    flagged = await clustering_queries.flagged_for_network_async(
        network,
        raise_on_error=True,
    )
    budget = settings.NOTIFY_CONTRACT_ANOMALY_MAX_ALERTS_PER_TICK
    attempts = 0
    for tx_hash, rows in flagged.items():
        if attempts >= budget:
            logger.warning(
                "contract_anomaly poll: per-tick alert cap (%d) reached; remaining "
                "findings drain on subsequent ticks",
                budget,
            )
            break
        try:
            winner = ca.resolve(rows)
            if winner is None:
                continue
            raw_band = winner.get("risk_band")
            band = raw_band.value if hasattr(raw_band, "value") else str(raw_band or "")
            dispatches = triggers.resolve_dispatch(band, "contract_anomaly")
            if not dispatches:
                continue  # this (band, contract_anomaly) isn't routed anywhere
            try:
                payload = build_contract_anomaly_alert(tx_hash, network, winner)
            except Exception:
                # Recall-first: a payload-build error on a ROUTED finding must not
                # drop the alert. Fall back to a minimal payload carrying the
                # projected band/score (the rich evidence fields are best-effort).
                logger.exception(
                    "contract_anomaly poll: degraded payload for %s (full build failed)",
                    tx_hash,
                )
                payload = build_degraded_contract_anomaly_alert(
                    tx_hash,
                    network,
                    band,
                    winner.get("score"),
                )
            status = await _deliver_with_dedup(
                network,
                tx_hash,
                band,
                payload,
                dispatches,
                source="contract_anomaly",
            )
            if status != DELIVER_DUPLICATE:
                attempts += 1  # a real send attempt (sent or failed) spends budget
        except Exception:
            logger.exception("contract_anomaly poll: skipping %s", tx_hash)


async def _contract_anomaly_loop() -> None:
    logger.info(
        "contract_anomaly poller started (interval=%ss)",
        settings.NOTIFY_CONTRACT_ANOMALY_POLL_SECONDS,
    )
    while True:
        try:
            await _contract_anomaly_tick()
        except Exception:
            # exc_info so a fetch/logic failure is diagnosable (the sidecar reads
            # RAISE up to here by design); the loop retries on the next tick so a
            # transient sidecar outage self-heals without dropping the alert path.
            logger.exception("contract_anomaly poller error")
        await asyncio.sleep(settings.NOTIFY_CONTRACT_ANOMALY_POLL_SECONDS)


def start() -> None:
    """Schedule the report loop (always) plus, when the clustering sidecar is
    enabled, the contract_anomaly poller. Idempotent: each task is checked
    INDEPENDENTLY so restarting one never skips or double-spawns the other (a
    single ``_task``-only guard could leave the poller unstarted, or orphan a
    still-running poller behind a new one)."""
    global _task, _ca_task
    if _task is not None and not _task.done():
        logger.warning("Notification scheduler already running; start() ignored")
    else:
        _task = asyncio.create_task(_loop())
    if settings.CLUSTERING_ENABLED and (_ca_task is None or _ca_task.done()):
        _ca_task = asyncio.create_task(_contract_anomaly_loop())


def stop() -> None:
    global _task, _ca_task
    if _task and not _task.done():
        _task.cancel()
    _task = None
    if _ca_task and not _ca_task.done():
        _ca_task.cancel()
    _ca_task = None
