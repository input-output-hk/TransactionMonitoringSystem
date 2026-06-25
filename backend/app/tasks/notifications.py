"""Background task: periodic notification report scheduler.

Wakes on a short interval, checks whether a report is due (the configured
frequency vs the persisted ``last_sent_at``), and when due assembles +
dispatches the report, then advances the boundary so a restart neither
double-sends nor skips a period.

Mirrors :mod:`app.tasks.analysis`: a module-level ``_task``, idempotent
``start()``/``stop()``, and a ``_loop`` with per-tick error isolation.
"""

import asyncio
import gzip
import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import postgres
from app.notifications import config, dispatcher, reports
from app.notifications.channels.base import Attachment

logger = logging.getLogger(__name__)

# Above this raw-CSV size, gzip the attachment so base64-encoded email stays
# under common SMTP limits; smaller reports ship as plain .csv (manual-export
# parity).
_CSV_GZIP_THRESHOLD_BYTES = 1_000_000

_task: "asyncio.Task | None" = None


async def _tick() -> None:
    if not config.report_enabled():
        return  # disabled — stay idle (config changes require a restart)

    cfg = config.periodic_report_config()
    network = settings.CARDANO_NETWORK
    now = datetime.now(timezone.utc)

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
        logger.warning(
            "periodic report due but no enabled channel has a recipient/URL; skipping"
        )
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
        "Periodic report sent for %s (window=%dd, channels=%s)",
        network, window_days, [d.channel for d in dispatches],
    )


async def _loop() -> None:
    logger.info(
        "Periodic report scheduler started (check interval=%ss)",
        settings.NOTIFY_REPORT_CHECK_INTERVAL_SECONDS,
    )
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.error("Periodic report scheduler error: %r", e)
        await asyncio.sleep(settings.NOTIFY_REPORT_CHECK_INTERVAL_SECONDS)


def start() -> None:
    """Schedule the report loop as a background task (idempotent)."""
    global _task
    if _task is not None and not _task.done():
        logger.warning("Notification scheduler already running; start() ignored")
        return
    _task = asyncio.create_task(_loop())


def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
