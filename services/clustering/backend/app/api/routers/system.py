"""Liveness/readiness probes — the only router WITHOUT the API-key dependency,
so Docker healthchecks and load-balancer probes work unauthenticated."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.api.deps import RepoDep
from app.api.schemas import ConfigOut, HealthOut, ReadyOut
from app.config import get_settings
from app.storage.protocol import Repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut)
def health() -> dict[str, str]:
    """Liveness: the process is up. Does not touch ClickHouse (see /ready)."""
    return {"status": "ok"}


@router.get("/config", response_model=ConfigOut)
def config() -> ConfigOut:
    """Read-only deployment config the UI reads to shape its onboarding form.

    host_backed → the engine reads txs from the host tables (no per-contract
    download), so fits run over the rolling window window_txs and a per-contract
    "max txs" has no effect; the form hides it. Non-sensitive, so it sits on the
    unauthenticated probe router (reachable only through the host's authed
    reverse-proxy anyway)."""
    s = get_settings()
    return ConfigOut(
        host_backed=s.host_backed,
        window_txs=s.clustering_window_txs,
        history_source=s.history_source if s.history_enabled else "",
    )


@router.get("/ready", response_model=ReadyOut)
def ready(repo: Repo = RepoDep) -> dict[str, str]:
    """Readiness: ClickHouse is reachable. 503 if not (detail kept generic)."""
    try:
        ok = repo.ping()
    except Exception as exc:  # pragma: no cover - reports connectivity issues
        logger.warning("readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail="clickhouse unavailable") from exc
    return {"status": "ready" if ok else "degraded"}
