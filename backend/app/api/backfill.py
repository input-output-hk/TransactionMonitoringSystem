"""Operator-initiated historical backfill of an address's transactions.

``POST /api/v1/backfill`` starts a background job that pulls an address's latest
transactions into ClickHouse via Kupo (the address index) + Ogmios block
re-fetch (see ``ingestion/address_backfill.py``), so a contract whose activity
predates this node's tip-forward sync can be onboarded. ``GET
/api/v1/backfill/{address}`` reports the job's status and summary.

The job store is in-memory: a single-process deploy (ADR-005) needs nothing more,
and a restart forgetting finished jobs is harmless because the ingested rows are
already durable in ClickHouse and a re-run is idempotent (ReplacingMergeTree).
This is deliberately operator-initiated, not automatic: an old, sparse address
can span a wide slot range, so kicking off that block scan is an explicit action.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Security
from pydantic import BaseModel, Field

from app.api._params import ADDRESS_RE
from app.auth import verify_api_key
from app.config import settings
from app.ingestion.address_backfill import backfill_address
from app.utils.bech32 import address_network_class

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backfill", tags=["backfill"])


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


@dataclass(slots=True)
class _Job:
    status: str  # "running" | "done" | "failed"
    address: str
    network: str
    max_txs: int
    started_at: str
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
        "started_at": job.started_at,
        "result": job.result,
        "error": job.error,
    }


async def _run(job: _Job) -> None:
    """Run the backfill and record its outcome on the job. Never raises: the
    result is polled via GET, so a failure becomes ``status='failed'`` with the
    message rather than an unobserved task exception."""
    try:
        result = await backfill_address(
            job.address,
            network=job.network,
            max_txs=job.max_txs,
            progress=lambda m: logger.info("backfill[%s…]: %s", job.address[:24], m),
        )
        job.result = {
            "requested_txs": result.requested_txs,
            "txs_ingested": result.txs_ingested,
            "blocks_scanned": result.blocks_scanned,
            "missing_tx_hashes": result.missing_tx_hashes,
        }
        job.status = "done"
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        logger.exception("backfill failed for %s", job.address)


@router.post("", status_code=202, dependencies=[Security(verify_api_key)])
async def start_backfill(req: BackfillRequest) -> dict:
    """Start a backfill for ``address`` (returns 202 with the job view).

    422 on a malformed address or a mainnet/testnet mismatch with the configured
    network, 503 when Kupo is not configured, 409 when a backfill for the same
    address is already running.
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
    job = _Job(
        status="running",
        address=req.address,
        network=network,
        max_txs=max_txs,
        started_at=datetime.now(UTC).isoformat(),
    )
    job.task = asyncio.create_task(_run(job))
    _jobs[key] = job
    return _public(job)


@router.get("/{address}", dependencies=[Security(verify_api_key)])
async def backfill_status(address: str) -> dict:
    """Status + summary of the most recent backfill for ``address`` (404 if none)."""
    job = _jobs.get((settings.CARDANO_NETWORK, address))
    if job is None:
        raise HTTPException(status_code=404, detail="No backfill job for this address")
    return _public(job)
