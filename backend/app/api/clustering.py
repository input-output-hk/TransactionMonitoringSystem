"""Reverse-proxy to the optional clustering sidecar's API.

The host SPA's rich Validators / cluster-graph / anomaly-table views call the
sidecar through this proxy so they stay same-origin and session-authenticated
(the SPA never talks to the sidecar directly, and the sidecar is not exposed
publicly). Every ``/api/clustering/<path>`` request is forwarded to the
sidecar's ``/api/v1/<path>`` verbatim (method, query, body), gated by
``CLUSTERING_ENABLED``.

The proxy is authed with the host's session (``verify_api_key``); the sidecar
itself runs zero-config on the internal network, so no credential is forwarded.
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, Security

from app.auth import verify_api_key
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clustering", tags=["clustering"])

# Generous timeout: cluster-graph / projection reads can be a few seconds on a
# large run, but bounded so a wedged sidecar can't hang an API worker forever.
_TIMEOUT = httpx.Timeout(30.0)
# Only safe content negotiation headers are forwarded; hop-by-hop headers and
# the host's own auth cookies/keys are intentionally dropped.
_FORWARD_HEADERS = ("content-type", "accept")


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PATCH", "DELETE"],
    dependencies=[Security(verify_api_key)],
)
async def proxy(path: str, request: Request) -> Response:
    if not settings.CLUSTERING_ENABLED:
        raise HTTPException(status_code=503, detail="Clustering module is not enabled")
    # Constrain the forwarded path to the /api/v1 namespace. FastAPI decodes
    # %2f to '/', so a ``..`` segment could otherwise climb out of the prefix
    # and reach an arbitrary path on the sidecar host. Reject any traversal
    # segment rather than trusting the upstream to normalize it away.
    if ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="Invalid clustering path")
    url = f"{settings.CLUSTERING_SIDECAR_URL.rstrip('/')}/api/v1/{path}"
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items() if k.lower() in _FORWARD_HEADERS
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            upstream = await client.request(
                request.method, url,
                params=request.query_params, content=body, headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("clustering proxy to %s failed: %s", url, exc)
        raise HTTPException(status_code=502, detail="Clustering sidecar unreachable")
    # Pass the upstream body/status straight through; only the content-type is
    # echoed (other upstream headers are not meaningful through the proxy).
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
