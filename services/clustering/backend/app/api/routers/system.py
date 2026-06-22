"""Liveness/readiness probes — the only router WITHOUT the API-key dependency,
so Docker healthchecks and load-balancer probes work unauthenticated."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.api.deps import RepoDep
from app.api.schemas import HealthOut, ReadyOut
from app.storage.protocol import Repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut)
def health() -> dict[str, str]:
    """Liveness: the process is up. Does not touch ClickHouse (see /ready)."""
    return {"status": "ok"}


@router.get("/ready", response_model=ReadyOut)
def ready(repo: Repo = RepoDep) -> dict[str, str]:
    """Readiness: ClickHouse is reachable. 503 if not (detail kept generic)."""
    try:
        ok = repo.ping()
    except Exception as exc:  # pragma: no cover - reports connectivity issues
        logger.warning("readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail="clickhouse unavailable") from exc
    return {"status": "ready" if ok else "degraded"}
