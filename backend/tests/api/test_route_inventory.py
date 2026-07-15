"""Route-inventory guard: every REST resource mounts under /api/v1.

The /api/v1 cut is enforced structurally rather than by convention: a new
router registered on the app directly (instead of on the api_v1 router in
main.py) would silently create a second, unversioned convention, which is
exactly the drift the versioned mount exists to prevent. Health probes, the
WebSocket handshake, and the SPA static mounts are the only sanctioned
root-level surfaces.
"""

from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount

from app.main import app

# Root-level surfaces that are deliberately NOT versioned: infrastructure
# probes for load balancers, the WS handshake (browsers cannot send custom
# headers there), and the built SPA.
_SANCTIONED_ROOT_PATHS = {
    "/",
    "/health",
    "/health/ready",
    "/health/detail",
    "/ws",
    "/{full_path:path}",  # SPA client-route fallback
}


def test_every_rest_route_is_versioned():
    unversioned = []
    for route in app.routes:
        if isinstance(route, APIWebSocketRoute):
            assert route.path in _SANCTIONED_ROOT_PATHS, route.path
            continue
        if isinstance(route, Mount):
            # StaticFiles mounts for the SPA (present only when built).
            continue
        if isinstance(route, APIRoute):
            if route.path.startswith("/api/v1/"):
                continue
            if route.path in _SANCTIONED_ROOT_PATHS:
                continue
            unversioned.append(route.path)
    assert unversioned == [], f"routes outside /api/v1: {unversioned}"


def test_no_legacy_unversioned_api_prefix():
    legacy = [
        r.path
        for r in app.routes
        if isinstance(r, APIRoute)
        and r.path.startswith("/api/")
        and not r.path.startswith("/api/v1/")
    ]
    assert legacy == [], f"legacy /api/* routes without version: {legacy}"
