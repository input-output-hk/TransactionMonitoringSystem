# Operations

How to run the clustering module securely. For what the endpoints do, see
[api.md](api.md); for how the module is wired into the host deployment (the
`clustering` compose profile, `CLUSTERING_ENABLED`), see the module
[README](../README.md).

## Enabling authentication

The module's API is unauthenticated by default so local and demo runs stay
zero-config. In the integrated deployment it is not published on its own: it
sits on the compose network behind the host's `/api/clustering` reverse proxy.
Any deployment where the module's port is reachable beyond that boundary must
enable authentication.

Three settings work together (engine names, with the compose-level variable
that feeds each one in the integrated deployment):

| Engine setting | Compose variable | Effect |
|---|---|---|
| `API_KEY` | `CLUSTERING_API_KEY` | When set, every endpoint except `/api/health` and `/api/ready` (and their `/api/v1` aliases) requires the header `X-API-Key: <key>`, else **401**. |
| `MODEL_SIGNING_KEYS` | `MODEL_SIGNING_KEYS` | Comma-separated HMAC keys for stored model blobs: sign with the first, verify against any (rotation). Unsigned blobs are pickle, that is code execution on load, so this is required wherever the ClickHouse server is not fully trusted. |
| `REQUIRE_AUTH` | `CLUSTERING_REQUIRE_AUTH` | Production safety switch: when true, startup refuses to boot unless both `API_KEY` and `MODEL_SIGNING_KEYS` are set. Set it for every network-exposed deployment so a missing key is a loud boot failure, not a silently open API. |

Steps for the integrated (compose) deployment:

1. Set `CLUSTERING_API_KEY`, `MODEL_SIGNING_KEYS`, and `CLUSTERING_REQUIRE_AUTH=1`
   in `.env` (see `.env.example`).
2. Restart the `clustering` service and the host backend.
3. Verify: a direct request without the header must return **401**, and the
   dashboard's clustering pages must still load through the proxy.

The SPA never holds the key: the host's `/api/clustering` proxy injects
`X-API-Key` into the forwarded request server-side (the host reads it as
`CLUSTERING_SIDECAR_API_KEY`, which the compose file sets from the same
`CLUSTERING_API_KEY` value), so the browser never sees it.

## CORS

`CORS_ORIGINS` (compose: `CLUSTERING_CORS_ORIGINS`) is a comma-separated origin
allowlist; empty means same-origin only, and there is no wildcard. The
integrated deployment does not need it, since the SPA reaches the module
through the host proxy on the same origin.

## Health probes

`GET /api/health` (liveness, no DB access) and `GET /api/ready` (readiness,
pings ClickHouse, **503** when unreachable) are auth-exempt by construction, so
orchestrator probes keep working with authentication enabled.
