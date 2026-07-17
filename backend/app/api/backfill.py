"""Operator-initiated historical backfill of an address's transactions.

``POST /api/v1/backfill`` starts a background job that pulls an address's latest
transactions into ClickHouse via Kupo (the address index) + Ogmios block
re-fetch (see ``ingestion/address_backfill.py``), so a contract whose activity
predates this node's tip-forward sync can be onboarded. ``GET
/api/v1/backfill/{address}`` reports the job's status and summary.

Starting a backfill is a heavy, state-changing operator action, so it is gated by
``require_admin_or_api_key`` (an Admin session or an API key; a non-admin Reviewer
session is rejected) and writes an audit row, matching the clustering proxy's
mutate path. Reading a job's status is a plain authenticated read.

The job store is in-memory: a single-process deploy (ADR-005) needs nothing more,
and a restart forgetting finished jobs is harmless because the ingested rows are
already durable in ClickHouse and a re-run is idempotent (ReplacingMergeTree).
Finished jobs are evicted past ``BACKFILL_JOB_RETENTION`` so the store cannot grow
without bound. Each scan is bounded by ``BACKFILL_TIMEOUT_SECONDS`` (a timed-out
job becomes ``failed``, which also releases the same-address 409 guard), and at
most ``BACKFILL_MAX_CONCURRENT`` scans run at once. This is deliberately
operator-initiated, not automatic: an old, sparse address can span a wide slot
range, so kicking off that block scan is an explicit action.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from pydantic import BaseModel, Field

from app import audit
from app.api._params import ADDRESS_RE
from app.auth import verify_api_key
from app.auth.deps import require_admin_or_api_key
from app.config import settings
from app.ingestion.address_backfill import BackfillError, backfill_address
from app.ingestion.kupo_client import KupoError, KupoUnavailable
from app.utils.bech32 import address_network_class

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backfill", tags=["backfill"])

# Chars of the address echoed in progress log lines (see address_backfill._ADDR_PREVIEW).
_ADDR_PREVIEW = 24


def _network_mismatch(address: str, network: str) -> str | None:
    """A client-facing error string when the address's network class cannot match
    the configured network, else None. Every testnet shares network id 0, so this
    catches only the mainnet-vs-testnet class error (the common mistake, e.g. a
    mainnet contract submitted to a preprod instance), NOT preprod-vs-preview."""
    addr_class = address_network_class(address)
    if addr_class is None:
        return None
    expected = "mainnet" if network == "mainnet" else "testnet"
    if addr_class != expected:
        return (
            f"Address is a {addr_class} address but this instance monitors "
            f"{network} (a {expected} network); a backfill would find nothing."
        )
    return None


class BackfillRequest(BaseModel):
    address: str
    # None → BACKFILL_DEFAULT_MAX_TXS; the handler clamps to BACKFILL_MAX_TXS_CAP.
    max_txs: int | None = Field(default=None, ge=1)
    # Optional upper slot bound: only transactions strictly below this slot are
    # backfilled, so a caller can reach history OLDER than the address's recent
    # activity (the default latest-N walk would just re-cover what tip-forward
    # sync already ingested). None preserves the original latest-N behavior.
    # Consumed by the clustering sidecar's kupo history backfill.
    created_before_slot: int | None = Field(default=None, ge=1)


@dataclass(slots=True)
class _Job:
    status: str  # "running" | "done" | "failed"
    address: str
    network: str
    max_txs: int
    started_at: str
    created_before_slot: int | None = None
    result: dict | None = None
    error: str | None = None
    # Held so the fire-and-forget task is not garbage-collected mid-run.
    task: asyncio.Task | None = field(default=None, repr=False)


# Keyed by (network, address). Module-level, single-process (see module docstring).
_jobs: dict[tuple[str, str], _Job] = {}


def _public(job: _Job) -> dict:
    """The client-facing view of a job (drops the internal task handle)."""
    return {
        "status": job.status,
        "address": job.address,
        "network": job.network,
        "max_txs": job.max_txs,
        "created_before_slot": job.created_before_slot,
        "started_at": job.started_at,
        "result": job.result,
        "error": job.error,
    }


def _safe_job_error(exc: BaseException) -> str:
    """A concise, client-safe error for the job view. The full exception (which
    can carry the internal Kupo URL or an upstream response body) is logged
    server-side; the client sees only a stable category string, never the raw
    message. Mirrors the clustering sidecar's ``_safe_error`` discipline."""
    if isinstance(exc, TimeoutError):
        return f"Backfill exceeded the {settings.BACKFILL_TIMEOUT_SECONDS:g}s time limit"
    if isinstance(exc, KupoUnavailable):
        return "Backfill unavailable: Kupo is not configured"
    if isinstance(exc, KupoError):
        return "Kupo request failed; see server logs"
    if isinstance(exc, BackfillError):
        return "Backfill failed during the chain scan; see server logs"
    return "Backfill failed unexpectedly; see server logs"


def _evict_finished_jobs() -> None:
    """Bound the in-memory store: drop the oldest finished/failed jobs beyond
    ``BACKFILL_JOB_RETENTION``. Running jobs are never evicted (their task and the
    409 guard depend on them). ``started_at`` is an ISO-8601 UTC string, so a
    lexical sort is chronological."""
    finished = [(key, job) for key, job in _jobs.items() if job.status != "running"]
    excess = len(finished) - settings.BACKFILL_JOB_RETENTION
    if excess <= 0:
        return
    finished.sort(key=lambda kv: kv[1].started_at)  # oldest first
    for key, _job in finished[:excess]:
        _jobs.pop(key, None)


async def _run(job: _Job) -> None:
    """Run the backfill and record its outcome on the job. Never raises: the
    result is polled via GET, so a failure becomes ``status='failed'`` with a
    client-safe message rather than an unobserved task exception. The scan is
    wrapped in a hard timeout so a stuck Ogmios cannot pin the job (and the
    same-address 409 guard) indefinitely."""
    try:
        result = await asyncio.wait_for(
            backfill_address(
                job.address,
                network=job.network,
                max_txs=job.max_txs,
                created_before_slot=job.created_before_slot,
                progress=lambda m: logger.info("backfill[%s…]: %s", job.address[:_ADDR_PREVIEW], m),
            ),
            timeout=settings.BACKFILL_TIMEOUT_SECONDS,
        )
        job.result = {
            "requested_txs": result.requested_txs,
            "txs_ingested": result.txs_ingested,
            "blocks_scanned": result.blocks_scanned,
            "missing_tx_hashes": result.missing_tx_hashes,
            "complete": result.complete,
            "degraded_reason": result.degraded_reason,
        }
        job.status = "done"
    except Exception as exc:
        job.status = "failed"
        job.error = _safe_job_error(exc)
        logger.exception("backfill failed for %s", job.address)


@router.post("", status_code=202)
async def start_backfill(
    req: BackfillRequest,
    request: Request,
    principal: str = Depends(require_admin_or_api_key),
) -> dict:
    """Start a backfill for ``address`` (returns 202 with the job view).

    422 on a malformed address or a mainnet/testnet mismatch with the configured
    network, 503 when Kupo is not configured, 409 when a backfill for the same
    address is already running, 429 when the global concurrent-scan limit is
    reached. Requires an Admin session or an API key.
    """
    if not ADDRESS_RE.match(req.address):
        raise HTTPException(status_code=422, detail="Invalid address format")
    network = settings.CARDANO_NETWORK
    mismatch = _network_mismatch(req.address, network)
    if mismatch is not None:
        raise HTTPException(status_code=422, detail=mismatch)
    if not settings.KUPO_URL:
        raise HTTPException(
            status_code=503, detail="Backfill unavailable: KUPO_URL is not configured"
        )
    max_txs = min(req.max_txs or settings.BACKFILL_DEFAULT_MAX_TXS, settings.BACKFILL_MAX_TXS_CAP)
    key = (network, req.address)
    existing = _jobs.get(key)
    if existing is not None and existing.status == "running":
        raise HTTPException(
            status_code=409, detail="A backfill for this address is already running"
        )
    running = sum(1 for job in _jobs.values() if job.status == "running")
    if running >= settings.BACKFILL_MAX_CONCURRENT:
        raise HTTPException(
            status_code=429,
            detail="Too many backfills already running; retry once one finishes",
        )
    await audit.record(
        event_type="address_backfill",
        action="start",
        entity_type="address",
        entity_id=req.address,
        details={
            "network": network,
            "max_txs": max_txs,
            "created_before_slot": req.created_before_slot,
        },
        request=request,
        actor=audit.actor_from_principal(principal),
    )
    job = _Job(
        status="running",
        address=req.address,
        network=network,
        max_txs=max_txs,
        created_before_slot=req.created_before_slot,
        started_at=datetime.now(UTC).isoformat(),
    )
    job.task = asyncio.create_task(_run(job))
    _jobs[key] = job
    _evict_finished_jobs()
    return _public(job)


@router.get("/{address}", dependencies=[Security(verify_api_key)])
async def backfill_status(address: str) -> dict:
    """Status + summary of the most recent backfill for ``address`` (404 if none)."""
    job = _jobs.get((settings.CARDANO_NETWORK, address))
    if job is None:
        raise HTTPException(status_code=404, detail="No backfill job for this address")
    return _public(job)
