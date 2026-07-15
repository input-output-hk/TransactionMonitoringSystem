# Runbook: Transaction Monitoring System

## Prerequisites

### 1. Cardano node + Ogmios (required)

The TMS connects to a Cardano node through Ogmios, a WebSocket bridge. You must have both running and reachable **before** starting the TMS.

Two options:

- **External infrastructure (recommended for production/staging):** run node + Ogmios separately and point `OGMIOS_WS_URL` at the remote endpoint. The details below describe this path.
- **Bundled local stack (development only):** `docker-compose.yml` includes `cardano-node` and `ogmios` services gated behind the `ingestion` profile. Start with `docker-compose --profile ingestion up`. Requires a populated config directory at `./cardano-config/preprod/` (override with `CARDANO_CONFIG_DIR`) containing `config.json` and `topology.json`, plus ~30 GB disk and a multi-hour initial chain sync. Leave `OGMIOS_WS_URL=ws://localhost:1337` (the default).

| Component | Version | Notes |
|---|---|---|
| cardano-node | 11.0.1 | Must be fully synced to the target network. 11.0.1 is required for the van Rossem PV11 hard fork; 8.x/9.x/10.x nodes stall at the PV11 boundary |
| Ogmios | v6.14.0 | Must be running alongside the node, accessible over WebSocket |

Ogmios listens on port `1337` by default. Verify it is reachable:

```bash
curl -s --no-buffer -H "Connection: Upgrade" -H "Upgrade: websocket" \
  http://<ogmios-host>:1337/health
```

A healthy Ogmios returns a JSON object with `"networkSynchronization": 1` (or close to it).

### 2. Local machine

- Docker and Docker Compose
- Python 3.13+ (managed via uv)


## First-time setup

```bash
# 1. Clone the repository
git clone <repository-url>
cd TransactionMonitoringSystem

# 2. Copy and edit the configuration
cp .env.example .env
```

Config is layered:

- `.env`: shared across every network (DB ports, log level, API keys).
- `.env.preprod`, `.env.preview`, `.env.<name>`: per-network overrides. Each one sets `CARDANO_NETWORK`, `OGMIOS_WS_URL`, and `API_PORT` for that network.

Edit `.env` for anything shared, and create a per-network file for each Cardano network you want to point at:

```bash
# .env.preprod
CARDANO_NETWORK=preprod
OGMIOS_WS_URL=ws://<host>:1337
API_PORT=8000

# .env.preview
CARDANO_NETWORK=preview
OGMIOS_WS_URL=ws://<host>:1338
API_PORT=8001
```

Which file is applied is chosen at launch via `TMS_ENV`; unset defaults to `preprod`.

If you want API key authentication, set `API_KEYS` in `.env`:

```bash
API_KEYS=your-key-1,your-key-2
```

For open-API local testing, leave `API_KEYS` empty **and** export `TMS_ALLOW_DEV_MODE=1`. The app refuses to start with empty keys and no dev-mode flag; this guard prevents an accidental production deploy from silently running unauthenticated.


## Starting the system

### Option A: databases in Docker, app on host (recommended for development)

```bash
# Start PostgreSQL and ClickHouse
docker compose up -d

# Wait for containers to be healthy
docker compose ps

# Install Python dependencies (first time only)
uv sync

# Start the application (defaults to preprod; port comes from .env.preprod)
cd backend
uv run python run.py

# To run against preview instead (port comes from .env.preview):
TMS_ENV=preview python run.py
```

`run.py` binds uvicorn to `settings.API_PORT`. Raw `uvicorn` needs an
explicit `--port` on the CLI because it does not read pydantic settings.

### Option B: everything in Docker

```bash
docker compose --profile app up -d
```

The app container connects to the databases internally. `OGMIOS_WS_URL` must still point to your external Ogmios host.
`TMS_ENV` must be set in .env to select the correct network-specific environment file (.env.<TMS_ENV>).


## Verifying the system is working

### 1. Health check

The public `/health` endpoint is a minimal liveness probe and does not
require auth:

```bash
curl http://localhost:8000/health
# {"status":"healthy"}
```

For the operational detail (pipeline state, Ogmios sync lag, connection
count), call the authenticated `/health/detail` endpoint:

```bash
curl -H "X-API-Key: $TMS_API_KEY" http://localhost:8000/health/detail
```

Expected response when connected to Ogmios:

```json
{
  "status": "healthy",
  "network": "preprod",
  "pipeline_state": "OK",
  "ogmios": {
    "pipeline_state": "OK",
    "last_ogmios_msg_at": "2026-03-02T10:00:00+00:00",
    "last_processed_slot": 12345678
  }
}
```

`pipeline_state` values:
- `OK`: connected and receiving blocks
- `DEGRADED`: one connection has issues but chain sync is still running
- `DOWN`: chain sync is not running (circuit breaker open)
- `UNKNOWN`: startup in progress

### 2. Dashboard

Open `http://localhost:8000/` in a browser. The dashboard shows a live feed of incoming transactions.

### 3. First API call

```bash
# No key needed when running with empty API_KEYS + TMS_ALLOW_DEV_MODE=1
curl "http://localhost:8000/api/v1/transactions?limit=5&network=preprod"

# With a key
curl -H "X-API-Key: your-key" "http://localhost:8000/api/v1/transactions?limit=5&network=preprod"
```

If the system is running and connected, this returns the most recent confirmed transactions within a few seconds of startup.

### 4. Interactive API docs

`http://localhost:8000/docs`


## Stopping the system

```bash
# Stop the app: Ctrl+C in the terminal running uvicorn
# (graceful shutdown, drains connections and closes WebSockets)

# Stop database containers
docker compose stop

# Stop and remove containers (data is preserved in Docker volumes)
docker compose down
```


## Day-to-day operations

### Logs

```bash
# Application logs (if running on host): visible in the terminal
# Application logs (if running in Docker):
docker compose logs -f app

# Database logs
docker compose logs -f postgres
docker compose logs -f clickhouse
```

### Database access

```bash
# PostgreSQL shell (lifecycle state, sync checkpoint)
docker exec -it tms-postgres psql -U tms_user -d tms_db

# ClickHouse shell (transactions, analysis results)
docker exec -it tms-clickhouse clickhouse-client
```

Useful queries:

```sql
-- PostgreSQL: recent lifecycle activity
SELECT tx_id, status, first_seen_at, confirmed_at, latency_ms
FROM tx_lifecycle
ORDER BY first_seen_at DESC
LIMIT 20;

-- PostgreSQL: current sync checkpoint
SELECT * FROM sync_checkpoint;

-- ClickHouse: recent confirmed transactions
SELECT tx_hash, slot, block_height, fee, total_input_value, total_output_value
FROM transactions FINAL
ORDER BY slot DESC
LIMIT 10;

-- ClickHouse: risk score distribution
SELECT risk_band, count() AS n
FROM tx_class_scores FINAL
GROUP BY risk_band;

-- ClickHouse: top attack classes by volume
SELECT max_class, count() AS n, avg(max_score) AS avg_score
FROM tx_class_scores FINAL
WHERE max_score > 0
GROUP BY max_class
ORDER BY n DESC;
```

### Container status

```bash
docker compose ps
```

### Restart after a crash

The application reconnects to Ogmios automatically on restart using an exponential backoff circuit breaker. After a restart it reads the last saved `sync_checkpoint` from PostgreSQL and resumes from that slot. The checkpoint only advances after the block's ClickHouse insert succeeds (failed inserts retry with backoff, then force a reconnect and replay), so confirmed blocks are not lost across restarts or transient ClickHouse outages. Replayed blocks deduplicate via the ReplacingMergeTree schema.

```bash
# Restart just the app (databases keep running)
# Ctrl+C the uvicorn process, then:
cd backend
python run.py                     # preprod (default)
# or: TMS_ENV=preview python run.py
```


## Configuration reference

Variables are layered across files:

- `.env`: shared across all networks.
- `.env.<TMS_ENV>` (e.g. `.env.preprod`, `.env.preview`): per-network overrides; applied on top of `.env`. Defaults to `.env.preprod` when `TMS_ENV` is unset.
- Shell environment variables override both files.

| Variable | Default | Description |
|---|---|---|
| `CARDANO_NETWORK` | `preprod` | `mainnet`, `preprod`, or `preview` |
| `OGMIOS_WS_URL` | `ws://localhost:1337` | Ogmios WebSocket endpoint |
| `API_KEYS` | _(empty)_ | Comma-separated API keys. Empty = open access; requires `TMS_ALLOW_DEV_MODE=1` or the app refuses to start |
| `RATE_LIMIT_ENABLED` | `true` | Enable per-key sliding-window rate limiting |
| `RATE_LIMIT_REQUESTS` | `240` | Max requests per window per key |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window in seconds |
| `TRUSTED_PROXY_ENABLED` | `false` (`true` in compose) | Honour forwarded headers for client IPs (rate limiting, audit). Right-most-hop parsing; headers count only when the direct peer is inside `TRUSTED_PROXY_CIDRS` |
| `TRUSTED_PROXY_HOPS` | `1` | Trusted proxies appending to `X-Forwarded-For`; the client is HOPS entries from the right |
| `TRUSTED_PROXY_CIDRS` | loopback + RFC1918 | CIDRs whose direct connections may carry forwarded headers |
| `TRUSTED_PROXY_CLIENT_IP_HEADER` | _(empty)_ (`CF-Connecting-IP` in compose) | Edge-set single-value client IP header; wins over `X-Forwarded-For` when present and valid |
| `CORS_ALLOW_ORIGINS` | `*` | Dashboard origin(s), comma-separated. The app refuses to start with `*` or empty when API keys are configured (`TMS_ALLOW_DEV_MODE=1` overrides for local dev) |
| `TMS_API_DOCS_ENABLED` | `false` | Expose `/docs`, `/redoc`, `/openapi.json` on a keyed deployment (always on in dev mode) |
| `WS_HANDSHAKE_RATE_LIMIT_REQUESTS` | `30` | WebSocket handshake attempts per client IP per window |
| `WS_HANDSHAKE_RATE_LIMIT_WINDOW_SECONDS` | `60` | WebSocket handshake rate-limit window |
| `AUDIT_LOG_RETENTION_DAYS` | `0` | Prune audit rows older than N days; `0` keeps forever (audit rows are the suppression accountability record) |
| `STATS_CACHE_TTL_SECONDS` | `10` | In-process TTL for the dashboard stats aggregate; `0` disables |
| `RAW_FALLBACK_RETRY_SECONDS` | `30` | Wall-clock spacing between counted raw-store fallback attempts |
| `ROLLBACK_SCORE_REPURGE_DELAY_SECONDS` | `60` | Delay before the second `tx_class_scores` rollback purge pass |
| `ANALYSIS_ENGINE_ENABLED` | `true` | Run background risk scoring |
| `ANALYSIS_ENGINE_INTERVAL_SECONDS` | `30` | How often the engine polls for unscored transactions |
| `ANALYSIS_ENGINE_BATCH_SIZE` | `100` | Transactions scored per run |
| `LEADER_LOCK_ENABLED` | `true` | Gate ingestion + analysis behind a Postgres advisory lock; see "Running more than one instance" below |
| `LEADER_LOCK_KEY` | `8737367427` | Advisory lock key; leave unchanged unless it collides with another lock class |
| `LEADER_LOCK_RETRY_SECONDS` | `15` | How often a standby instance retries to become leader |
| `ANALYSIS_ENABLED` | `true` | Enable multi-class detection engine |
| `CYCLE_DETECTION_ENABLED` | `true` | Enable transfer graph cycle detection |
| `CYCLE_MAX_HOPS` | `6` | Maximum BFS depth for cycle detection |
| `CYCLE_MAX_FANOUT` | `50` | Maximum addresses tracked per BFS hop |
| `SANDWICH_SIMPLIFIED_ENABLED` | `true` | Enable structural sandwich pattern detection |
| `BASELINE_MIN_SAMPLES` | `200` | Minimum samples before per-entity baseline is valid |
| `SMTP_ENABLED` | `true` | Send magic-link emails over SMTP; `false` logs the link instead |
| `SMTP_HOST` / `SMTP_PORT` | `mailpit` / `1025` (compose) | SMTP relay. The compose default is the bundled Mailpit catch-all, see "Magic-link email in production" below |
| `SMTP_FROM_EMAIL` | `noreply@tms.local` | Sender address (code default; `.env.example` ships `noreply@example.com`). Not validated, but set a real domain in production: special-use domains like `.local`/`.test` are rejected by many receivers |
| `APP_BASE_URL` | `http://localhost:8000` | Base URL baked into emailed magic links. Must be the public dashboard URL in production or links will not resolve |
| `MAGIC_LINK_TTL_MINUTES` | `15` | Magic-link token lifetime |
| `MAGIC_LINK_PER_EMAIL_LIMIT` | `5` | Link requests per address per window (silent throttle, always on) |
| `RAW_STORE_ENABLED` | `true` | Write raw Ogmios payloads to filesystem |
| `RAW_STORE_PATH` | `./data/raw` | Root path for the Data Lake |
| `LIFECYCLE_PENDING_TTL_SECONDS` | `7200` | After this time a PENDING tx is marked DROPPED |
| `HOUSEKEEPING_INTERVAL_SECONDS` | `30` | Tick of the stale-PENDING sweep, retention, and auth purge; runs whether or not `ANALYSIS_ENGINE_ENABLED` is set |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Database variables (`POSTGRES_*`, `CLICKHOUSE_*`) default to the values used by Docker Compose and rarely need changing.

### Running more than one instance

The app is built to run as a single instance: the Ogmios chain-sync checkpoint and the analysis engine's poll watermark are both in-process state that assumes exactly one writer. A second live process advancing the same state would double-insert transactions and race the checkpoint update.

Ingestion, analysis, housekeeping, and the notification schedulers (periodic report, contract-anomaly poller) are gated behind a Postgres session-level advisory lock (`LEADER_LOCK_ENABLED`, on by default) so this is safe to get wrong: only the instance that holds the lock runs them; any other instance still serves the read-only API, dashboard, and WebSocket feed, and retries every `LEADER_LOCK_RETRY_SECONDS` to take over (for example after the leader is redeployed or crashes). There is no need to manually pick a leader or coordinate a rolling restart; whichever instance acquires the lock first wins, and a standby is promoted automatically once it frees up.

Two caveats for multi-instance deployments:

- The lock needs a direct Postgres session. A transaction-pooling proxy (PgBouncer in transaction mode) reassigns the server session between statements and silently breaks session-level advisory locks; point the app at the real server or use a session-mode pool.
- The notification config is cached in-process and refreshed only by the instance that handles an admin edit. If a load balancer routes the admin UI to a standby, the leader keeps alerting with its previous config until it restarts or handles an edit itself. Make notification-config edits against the leader, or restart the leader after editing.

### Running behind Cloudflare Tunnel

The compose deployment binds every port to loopback and expects a tunnel (cloudflared or similar) to terminate TLS in front of the app. Client-IP attribution works as follows: `TRUSTED_PROXY_ENABLED=true` lets the app honour forwarded headers, but only when the direct TCP peer is inside `TRUSTED_PROXY_CIDRS` (the tunnel connector / compose bridge), and the parser takes `CF-Connecting-IP` first (Cloudflare overwrites it per request) falling back to the right-most `X-Forwarded-For` hop. Client-writable left-most entries never win, so rate-limit buckets and audit rows cannot be spoofed.

WebSocket note: browsers cannot set custom headers on WS upgrades, so the dashboard passes `?api_key=` in the query string, which can land in proxy and access logs. Use a dedicated key for dashboards so it can be rotated independently of automation keys.

### Magic-link email in production

The compose stack bundles Mailpit as a catch-all SMTP sink for development: every email the app sends is captured and viewable at `http://127.0.0.1:8025`, and nothing is forwarded. That convenience is a liability once real users sign in, because magic-link emails are login credentials. If `SMTP_HOST` is left at its `mailpit` default in production, every sign-in link accumulates in an unauthenticated inbox on the host.

For production deployments:

1. Point `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (plus `SMTP_USE_TLS` or `SMTP_USE_STARTTLS`) at the customer's SMTP provider.
2. Set `APP_BASE_URL` to the public dashboard URL so emailed links resolve.
3. Stop the Mailpit container once the stack is up: `docker compose stop mailpit`. The app only requires it at startup ordering time; SMTP failures at runtime are tolerated (logged, silent 200 to the caller).

### Webhook notifications: payload and signature verification

The webhook channel POSTs each notification as JSON. The body is the payload record verbatim, discriminated by `notification_type`. An `immediate_alert` (one high-risk transaction, sent as it is scored):

```json
{
  "notification_type": "immediate_alert",
  "timestamp": "2026-07-14T09:15:02+00:00",
  "attack_class": "token_dust",
  "risk_score": 91.0,
  "risk_band": "Critical",
  "tx_hash": "3f2a...",
  "network": "preprod",
  "contributing_features": {"unique_assetclass_count": 0.97, "value_cbor_bytes": 0.88},
  "baseline_source": "per_script",
  "dashboard_url": "https://<your-dashboard>/attacks/3f2a..."
}
```

A `periodic_report` (scheduled digest) carries `report_window` (`{"from", "to"}` ISO timestamps), a `summary` block (`total_transactions_scored`, `alerts_by_band`, `alerts_by_class`, `false_positives_archived`), a `top_alerts` list, and `dashboard_url`.

Delivery semantics: transient failures (5xx, network) are retried a bounded number of times; a 4xx is treated as permanent and not retried. Answer with any 2xx quickly, then process asynchronously.

When a signing secret is configured (`WEBHOOK_SIGNING_SECRET`), every request carries an `X-TMS-Signature: sha256=<hexdigest>` header, the HMAC-SHA256 of the exact raw request body. Verify it before trusting the payload:

```python
import hashlib, hmac

def verify(secret: str, raw_body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # compare_digest, not ==, to avoid a timing oracle
    return hmac.compare_digest(expected, header)
```

Sign-then-verify works on the raw bytes: do not re-serialize the JSON before verifying, or key ordering and whitespace differences will produce a different digest. `backend/scripts/webhook_testing/` ships a reference receiver implementing exactly this check.

Egress guard: targets that resolve to loopback, private, link-local, or cloud-metadata addresses are refused at config time and re-checked at send time. Set `WEBHOOK_ALLOW_INTERNAL=true` only for local development against a receiver on your own machine.

### First admin: user bootstrap

A fresh install has zero users, and the dashboard's user management requires an existing Admin to invite anyone. The `create-admin` CLI breaks that chicken-and-egg: it creates (or promotes) an Admin user and issues an invite magic link, printing the link to stdout so bootstrap works even before SMTP is configured.

App running on the host:

```bash
cd backend && python -m app.cli create-admin admin@example.com "Full Name"
```

App running in Docker:

```bash
docker compose --profile app run --rm app python -m app.cli create-admin admin@example.com "Full Name"
```

Notes:

- The command is idempotent: if the email already exists the user is promoted to Admin (status untouched) and a fresh invite token is issued. Safe to re-run when a link expires (default TTL is `MAGIC_LINK_TTL_MINUTES`, 15 minutes).
- Add `--no-email` to skip the SMTP send and only print the link.
- Set `APP_BASE_URL` before running: the printed link is built from it, and the default `http://localhost:8000` produces links that do not resolve from anywhere else. The token itself is host-independent, so an already-issued link can be salvaged by swapping the host part, but fixing `APP_BASE_URL` is the real fix.
- Open the printed link in a browser to activate the account and start a session.

From there, all further users are created through the dashboard: an Admin invites them by email (role `Admin` or `Reviewer`), the invitee receives a magic link, and every later sign-in requests a fresh link from the login page. There are no passwords anywhere in the flow.


## Troubleshooting

### "Cannot connect to Ogmios"

```
ConnectionRefusedError / WebSocket connection failed
```

- Verify Ogmios is running: `curl http://<host>:1337/health`
- Check `OGMIOS_WS_URL` in `.env`: must be `ws://` not `http://`
- Check firewall rules between the TMS server and the Ogmios host
- The application will retry with exponential backoff; watch the logs for reconnection attempts

### `pipeline_state` is DEGRADED or DOWN

```bash
curl -H "X-API-Key: $TMS_API_KEY" http://localhost:8000/health/detail
```

Check the `ogmios` field for `circuit_breaker_chain` and `circuit_breaker_mempool`. If `OPEN`, the circuit breaker tripped after repeated failures. It will attempt a probe after a 2-minute cooldown automatically; no manual action needed unless the underlying connectivity problem persists.

### Database containers won't start

```bash
docker compose logs postgres
docker compose logs clickhouse
```

**Port conflict**: if ports 5433 or 9000 are in use on the host, change the host-side mapping in `docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:5434:5432"   # change 5433 to any free port
```

Update `POSTGRES_PORT` in `.env` to match.

### No transactions appearing

1. Check `pipeline_state` is `OK` at `/health/detail` (requires API key)
2. Ogmios may still be syncing; `networkSynchronization` in its health endpoint should be `1`
3. On Preprod, blocks arrive roughly every 20 seconds; wait at least one full block cycle
4. Check logs for parser errors: `grep -i error` in the application log

### Reset all data

**Destructive: deletes everything in the databases and the raw data volume.** Take a backup first (next section).

```bash
docker compose down -v
docker compose up -d
```

On next startup the application recreates all schemas automatically and begins syncing from the chain tip.

#### Network-scoped reset (preferred)

`docker compose down -v` wipes every network's data at once. When you only want to clear one network (the common case on a shared box running preprod and preview side by side), use the network-aware `scripts/reset.sh`, which issues scoped `DELETE ... WHERE network = ?` statements instead of dropping volumes:

```bash
./scripts/reset.sh                 # reset only the current TMS_ENV's network (safe default)
./scripts/reset.sh --network=preview   # reset a specific network
./scripts/reset.sh --all           # reset every network (equivalent scope to down -v, but keeps volumes)
./scripts/reset.sh --yes           # skip the confirmation prompt (automation)
```

The script honours `TMS_ENV`, so running it from a preview-configured terminal resets preview unless you override with `--network=`.

## Backup & restore

Run `./scripts/backup.sh [output-dir]` with the databases up. It produces:

- `postgres.sql.gz`: full dump of the operational DB (lifecycle, sync checkpoints, collisions, audit logs, entity state)
- `clickhouse/<table>.native.gz`: per-table Native-format export of the analytics warehouse, including `tx_class_scores` and `archived_alerts` (the detection product)
- `MANIFEST`: row counts at backup time

The raw store (Data Lake) is write-once files; back it up incrementally with rsync or restic against the `raw_store_data` volume (host runs: `RAW_STORE_PATH`). Schedule the script via cron on the host; daily is appropriate at preprod volume.

Restore:

1. Start empty databases (`docker compose up -d`), let the app create schemas once, then stop the app.
2. PostgreSQL: `gunzip -c postgres.sql.gz | docker exec -i tms-postgres psql -U $POSTGRES_USER $POSTGRES_DB`
3. ClickHouse, per table: `gunzip -c clickhouse/<t>.native.gz | docker exec -i tms-clickhouse clickhouse-client --query "INSERT INTO tms_analytics.<t> FORMAT Native"`
4. Restore the raw-store files into the volume, then start the app. The sync checkpoint in the Postgres dump makes ingestion resume from the backed-up slot; any gap replays from the chain.

## Schema migration (dedup-safe v2)

Deployments created before the ReplacingMergeTree schema must run the one-shot migration before the app will start (the startup guard refuses a legacy layout and names the script):

1. Stop ALL app instances sharing the ClickHouse database (preprod and preview).
2. Dry-run: `cd backend && python scripts/migrate_dedup_schema.py` (prints per-table row and duplicate counts).
3. Apply: `python scripts/migrate_dedup_schema.py --apply`
4. Restart the instances. Legacy data is preserved as `<table>__legacy_<date>`; drop those tables manually after a verification window.

## API quick reference

All endpoints accept `?network=preprod`, `?network=mainnet`, or `?network=preview`; defaults to the instance's `CARDANO_NETWORK`.
All endpoints require `X-API-Key: <key>`. For open-API dev mode, boot with empty `API_KEYS` **and** `TMS_ALLOW_DEV_MODE=1`.
Migration note: the header was renamed from `TMS-API-Key` and all REST paths moved from `/api/...` to `/api/v1/...` in the v1 versioning cut. Out-of-repo callers still sending the old header can bridge with `API_KEY_HEADER=TMS-API-Key` in the environment; there is no path bridge.

```bash
BASE=http://localhost:8000
KEY="X-API-Key: your-key"

# Recent transactions
curl -H "$KEY" "$BASE/api/v1/transactions?limit=20"

# Single transaction
curl -H "$KEY" "$BASE/api/v1/transactions/<tx_hash>"

# All transactions for an address
curl -H "$KEY" "$BASE/api/v1/transactions/address/<addr>"

# Pending (mempool) transactions
curl -H "$KEY" "$BASE/api/v1/lifecycle?status=PENDING"

# Transaction lifecycle state
curl -H "$KEY" "$BASE/api/v1/lifecycle/<tx_hash>"

# Risk analysis results
curl -H "$KEY" "$BASE/api/v1/analysis/results?risk_band=High&limit=50"

# Analysis result for a single transaction
curl -H "$KEY" "$BASE/api/v1/analysis/results/<tx_hash>"

# Aggregate stats
curl -H "$KEY" "$BASE/api/v1/transactions/stats/summary"
curl -H "$KEY" "$BASE/api/v1/lifecycle/stats/summary"
curl -H "$KEY" "$BASE/api/v1/analysis/stats"

# Health (no key required)
curl "$BASE/health"
```

### WebSocket feed

Connect to `ws://localhost:8000/ws` to receive real-time lifecycle events:

```json
{
  "type": "lifecycle",
  "data": {
    "event": "TX_CONFIRMED",
    "tx_id": "abc123...",
    "status": "CONFIRMED",
    "slot": 12345678,
    "latency_ms": 18400
  },
  "timestamp": "2026-03-02T10:00:00+00:00"
}
```

Event types: `TX_PENDING`, `TX_CONFIRMED`, `TX_ROLLED_BACK`.
