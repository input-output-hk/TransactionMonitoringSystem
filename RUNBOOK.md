# Runbook: Transaction Monitoring System

## Prerequisites

### 1. Cardano node + Ogmios (required)

The TMS connects to a Cardano node through Ogmios, a WebSocket bridge. You must have both running and reachable **before** starting the TMS.

Two options:

- **External infrastructure (recommended for production/staging):** run node + Ogmios separately and point `OGMIOS_WS_URL` at the remote endpoint. The details below describe this path.
- **Bundled local stack (development only):** `docker-compose.yml` includes `cardano-node`, `ogmios`, and `kupo` (the address→tx index backing `POST /api/v1/backfill`, configured via `KUPO_URL` / `KUPO_SINCE` / `KUPO_MATCH`) services gated behind the `ingestion` profile. Start with `docker-compose --profile ingestion up`. Requires a populated config directory at `./cardano-config/preprod/` (override with `CARDANO_CONFIG_DIR`) containing `config.json`, `topology.json`, and the four genesis files (`byron-genesis.json`, `shelley-genesis.json`, `alonzo-genesis.json`, `conway-genesis.json`), plus `checkpoints.json` and `peer-snapshot.json` where the network's config and topology reference them, all co-located. Run `./scripts/fetch-cardano-config.sh preprod` to download the set in one pass from the Cardano environments listing at https://book.world.dev.cardano.org/environments.html rather than picking files individually: `config.json` references the genesis files (and `checkpoints.json`, where used) by filename **and hash**, and `topology.json` references `peer-snapshot.json`, so a missing or edited file fails the hash check at node startup. Also needs ~30 GB disk and a multi-hour initial chain sync. Leave `OGMIOS_WS_URL=ws://localhost:1337` (the default).

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
- `.env.mainnet`, `.env.preprod`, `.env.preview`, `.env.<name>`: per-network overrides. Each one sets `CARDANO_NETWORK`, `OGMIOS_WS_URL`, and `API_PORT` for that network, and may also set `KUPO_URL` and `APP_BASE_URL`.

Edit `.env` for anything shared, and create a per-network file for each Cardano network you want to point at:

```bash
# .env.mainnet
CARDANO_NETWORK=mainnet
OGMIOS_WS_URL=ws://<host>:1337
API_PORT=8000

# .env.preprod
CARDANO_NETWORK=preprod
OGMIOS_WS_URL=ws://<host>:1337
API_PORT=8000

# .env.preview
CARDANO_NETWORK=preview
OGMIOS_WS_URL=ws://<host>:1338
API_PORT=8001
```

Which file is applied is chosen at launch via `TMS_ENV`; unset defaults to `preprod`. That default is the reason a mainnet deployment should carry an explicit `.env.mainnet` and `TMS_ENV=mainnet`: on a host that also has a `.env.preprod`, forgetting `TMS_ENV` silently layers preprod, which flips `CARDANO_NETWORK` and re-targets `scripts/reset.sh` (see [Reset all data](#reset-all-data)). Copy `.env.mainnet.example`, which documents the knobs whose defaults are sized for preprod rather than mainnet volume.

Every Compose service has a fixed `container_name` and takes its published ports from the top-level `.env`, so one host runs one network unless you give the second stack its own `COMPOSE_PROJECT_NAME` and distinct published ports throughout.

One caveat on Compose variables: `${...}` placeholders in `docker-compose.yml` are resolved by Compose from the shell and the **top-level `.env`** only. A per-network `.env.<name>` is passed into containers via `env_file`, so it never reaches interpolation.

Read the rule in that direction, because most app settings are affected: **anything listed under the app service's `environment:` block must be set in `.env` or the shell.** An `environment:` entry both overrides `env_file` and resolves only from `.env`, so a value put in a per-network file is silently replaced by the interpolation default. That covers `POSTGRES_*`, `CLICKHOUSE_*`, `SMTP_*`, `TRUSTED_PROXY_*`, `CORS_ALLOW_ORIGINS`, `CLUSTERING_*` and `RAW_STORE_ENABLED`, plus the container-level `API_PORT`, `KUPO_SINCE` and `KUPO_MATCH`.

Everything the `environment:` block does **not** name still comes from the layered `env_file`, and that is most of the configuration surface: `CARDANO_NETWORK`, `OGMIOS_WS_URL`, `LOG_LEVEL`, the `CH_RETENTION_DAYS_*` knobs and the `NOTIFY_*` / `WEBHOOK_*` settings all live there. Two settings in particular were deliberately left out of the block so they can vary per network:

- `KUPO_URL`: the backfill index differs per network.
- `APP_BASE_URL`: the public dashboard URL differs per network, and getting it wrong is silent (emailed magic links stop resolving).

`CORS_ALLOW_ORIGINS` is the one to watch, because it looks like it belongs in the same group. It stays pinned on purpose: its interpolation default (empty) is fail-closed, while the code default is `*`. Set it in `.env`; in a per-network file it is blanked, and the app then refuses to start whenever `API_KEYS` is set. `TRUSTED_PROXY_ENABLED` is pinned for the same kind of reason (Compose sends `true`, the code default is `false`).

`API_PORT` sits on both sides of that line, so it is worth stating plainly:

- Under Compose it selects the **published host port** via interpolation, so it must be set in `.env` (or the shell) to take effect. The container's own bind is pinned to 8000 in the compose file, because the container side of the port mapping, `EXPOSE`, and the healthcheck probe are all fixed at 8000.
- For a **host run**, `run.py` binds uvicorn to `settings.API_PORT`, which does come from the layered per-network file. That is what the `API_PORT` line in each per-network template is for.

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

`TMS_ENV` must be set in `.env` for this path: Compose reads that file to resolve the `env_file: .env.${TMS_ENV}` entry, and the resulting values are injected into the container as real environment variables.

Set it in the shell **as well** if you also run anything on the host. `TMS_ENV` is read from the process environment before any dotenv file is loaded ([config.py](backend/app/config.py) `_env_files`), so a value that lives only in `.env` is invisible to `run.py`, `app.cli`, and `scripts/reset.sh`: those fall back to `preprod`, ignore the per-network file, and take each setting from `.env` or its built-in default. On a mainnet host that means `reset.sh` scopes correctly only because `CARDANO_NETWORK` happens to default to `mainnet`, and it silently re-scopes to preprod the moment a `.env.preprod` exists. Add `export TMS_ENV=mainnet` to the deploy shell profile. Commands run via `docker compose exec` are unaffected, since they inherit the container's environment.


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

#### Log rotation

Under Docker Compose, container logs are rotated by the shared `json-file`
logging driver defined once at the top of `docker-compose.yml` (`x-logging`)
and applied to every service except the optional `mailpit` dev mail-catcher:
`max-size: "50m"`, `max-file: "5"`, so each container keeps at most ~250 MB
of logs before the oldest chunk is discarded.
Without it the `json-file` driver grows unbounded, which is a real
disk-exhaustion risk on a long-running deployment; leave it in place. To keep
more or less history, edit the `x-logging` anchor (a single edit propagates to
all services).

When the app runs on the host (Option A) rather than in a container, log
rotation is the operator's responsibility: pipe stdout to a rotated file
(`logrotate`, `svlogd`, or your service manager's journal) or run it under a
supervisor that handles rotation. Set `LOG_FORMAT=json` for a log collector.

### Database access

```bash
# PostgreSQL shell (lifecycle state, sync checkpoint)
docker exec -it tms-postgres sh -c \
  'exec psql -U "${POSTGRES_USER:-tms_user}" -d "${POSTGRES_DB:-tms_db}"'

# ClickHouse shell (transactions, analysis results).
# The credentials are resolved inside the container, from the environment
# Compose populated: clickhouse-client reads CLICKHOUSE_PASSWORD from its own
# environment, so no secret crosses from the host and none appears in `ps`.
# Do NOT add `-e CLICKHOUSE_PASSWORD`: when the variable is unset in your shell
# that flag strips the container's own value instead of forwarding it, and the
# shell fails with `Code: 516 Authentication failed` on a correctly configured
# server. Single quotes are deliberate: the container's shell does the expansion.
docker exec -it tms-clickhouse sh -c \
  'exec clickhouse-client --user "${CLICKHOUSE_USER:-default}"'
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

### ClickHouse disk use: its own logs, not your data

ClickHouse's system log tables are the largest thing in the ClickHouse volume on
a long-running deployment, by an order of magnitude. Measured on mainnet 15 days
after go-live: 32 GB of volume, of which about 30 GB was `system.*` log tables
and about 1 GB was actual transaction data. `system.text_log` alone held 14.2 GB
across 380 million rows, because the stock image runs it at `trace` level. Eight
of the nine tables targeted at that time shipped with no TTL at all (`text_log`,
`trace_log`, `query_log`, `asynchronous_metric_log`, `metric_log`, `part_log`,
`query_views_log`, `query_metric_log`; only `processors_profile_log` carried
one, at 30 days). That is roughly 2.0 GiB/day of growth, against ~62 MiB/day for
the detection data itself: about thirty to one. Two more tables were added to
the list on 2026-08-03, bringing it to eleven, for the reason given at the end
of this section.

`clickhouse/config.d/log-retention.xml` fixes this for new deployments: it drops
`text_log` to `information` and puts a 7-day TTL on the eleven log tables this
stack actually populates. It also caps the server's own log FILES, which are a
separate concern from the tables: stock is `trace` level rotating at
1000 MB x 10 (up to ~10 GB), and the file lowers both together, to `information`
at 100 MB x 3. Both halves have to move at once, because a 33x smaller cap at
unchanged `trace` verbosity would hold only hours, which would defeat the 7-day
window the tables are set to.

Disabling the sampling profiler takes **two** settings in **two** files, because
query threads and background threads are governed separately and each knob is a
silent no-op in the other's file:

| Thread population | Setting | File |
|---|---|---|
| Query threads | `query_profiler_real_time_period_ns` | `clickhouse/users.d/query-profiler.xml` (user profile) |
| Background threads | `global_profiler_real_time_period_ns` | `clickhouse/config.d/log-retention.xml` (server) |

The split matters more than it looks: `trace_log`'s `Real` rows look like query
profiling and are not. Measured on mainnet with only the user-profile knob live,
**11,046 of 11,088 Real samples in three minutes carried no `query_id`**, so
99.6% were background threads and the user-profile setting alone removed 0.4%
of them. Setting one without the other leaves nearly all the volume in place.

**Measured sizes at 7-day steady state**, taken on mainnet on 2026-08-03 with
both files live. They span three orders of magnitude, so there is no single
per-table figure:

| Table | Steady state | Rate |
|---|---|---|
| `query_log` | ~1.56 GiB | 838 K rows/day at 286.1 B/row |
| `trace_log` | ~750 MiB | memory traces only; query profiler off |
| `text_log` | ~2.8 MiB | 8.6 K rows/day at 46.6 B/row |
| everything else | ~120 MiB | `asynchronous_metric_log` dominates |

Total lands near 2.4 GiB. The `text_log` figure is the one worth understanding:
at the stock `trace` level it took 22.3 M rows/day, about 1.04 GiB/day, which
would be ~7.3 GiB at a 7-day TTL and the largest object in the volume. At
`information` it measures 6 rows/minute with zero Trace and zero Debug rows,
roughly 2,500x less. Anyone re-deriving the sizing from the pre-fix tables will
overestimate `text_log` by that factor.

`trace_log` is sampling-profiler output rather than diagnostics. Before either
profiler was disabled it broke down as Real 59.2%, Memory 20.8%,
MemoryPeak 19.7%, CPU 0.2%. The Real and CPU rows are the sampling described
above, overwhelmingly from background threads, and had never been read here:
across the whole `query_log` history the only statements touching
`system.trace_log` were a TTL inspection and the `TRUNCATE` from the 2026-07-30
cleanup. Both are off.

The Memory half is kept deliberately: this container runs under a 4 GiB
`mem_limit` it has already exceeded once mid-mutation, and memory traces are
what explain that. It is what remains in the table.

To profile a slow query, re-enable per session with
`SET query_profiler_real_time_period_ns = 1000000000` rather than editing either
file. That works because the session setting overrides the user profile;
background-thread sampling stays off, which is what you want when profiling a
specific query anyway.

`query_log` is the largest remaining table and is worth its size, being the one
that answers what ran, when and for how long. 77.5% of its rows are queries
under 10 ms, so `log_queries_min_query_duration_ms` would cut it to ~350 MiB;
that is left unset on purpose, because it also erases the query-volume signal
and a flood of fast queries is a real failure mode.

The config covers the tables this workload creates, not all 23 the server can
define. That set drifts: `error_log` and `background_schedule_pool_log` were
assumed empty in the first pass and had started being written by 2026-08-03.
After an image bump or a feature change, re-run the audit and add anything that
reports `NO TTL`:

```sql
SELECT name, if(position(engine_full,'TTL')>0,'has TTL','NO TTL')
FROM system.tables WHERE database='system' AND name LIKE '%log%'
```

Confirm the file is present before `docker compose up -d`. It is a bind mount, so
on a host where the path is missing Docker creates an empty **directory** at
`/etc/clickhouse-server/config.d/log-retention.xml`. ClickHouse only reads `*.xml`
FILES from `config.d/`, so it does not fail on the directory: it starts normally
with none of these limits applied, and the first symptom is the disk filling
weeks later. That silence is why the check is worth doing. A `git pull` deploy
gets the file for free; a hand-copied deploy needs it verified:

```bash
docker exec tms-clickhouse test -f \
  /etc/clickhouse-server/config.d/log-retention.xml && echo present
# And that it took effect (expect: information / 100M / 3):
docker exec tms-clickhouse clickhouse-client -q \
  "SELECT name, value FROM system.server_settings WHERE name IN ('logger.level','logger.size','logger.count') FORMAT TSV"
```

It is mounted as a single file rather than over `config.d/`, because bind-mounting
the directory would hide the image's own `docker_related_config.xml`.

To check what a running server is holding:

```bash
docker exec tms-clickhouse clickhouse-client -q "SELECT table, formatReadableSize(sum(bytes_on_disk)) AS size, sum(rows) AS rows FROM system.parts WHERE active AND database='system' GROUP BY table ORDER BY sum(bytes_on_disk) DESC LIMIT 10 FORMAT PrettyCompactMonoBlock"
```

On an EXISTING server the config alone reclaims nothing, because these settings
apply when ClickHouse CREATES a log table. A table that already exists with a
different TTL is not migrated in place: ClickHouse renames the mismatched table
to `<name>_0` and creates a fresh, empty one with the new TTL. Every table whose
stock TTL differs from 7 days is renamed, the fresh tables are empty and
correct, and every byte you are trying to reclaim is now sitting in the `_0`
tables, which carry no TTL and will never expire on their own. So after
restarting with the config mounted, the job is to drop the renamed tables:

**The rename is lazy**, triggered by the table's first write after restart
rather than by startup itself. A busy table is renamed within seconds, but a
rarely-written one is not: on mainnet on 2026-08-03, `error_log` still reported
`NO TTL` several minutes after the restart and was renamed only once an error
was provoked to force a flush. Two consequences. A post-restart audit can show a
table as unbounded when the config is actually correct, so re-check after the
table has been written to before concluding the setting failed. And `_0` tables
can appear later than the restart, so the drop below is worth re-running once
the server has been up for a while rather than only immediately after.

```bash
# What the renamed tables are still holding:
docker exec tms-clickhouse clickhouse-client -q "SELECT table, formatReadableSize(sum(bytes_on_disk)) AS size FROM system.parts WHERE active AND database='system' AND table LIKE '%\_0' GROUP BY table ORDER BY sum(bytes_on_disk) DESC FORMAT PrettyCompactMonoBlock"

# Drop them. DROP, not TRUNCATE: a truncated `_0` table would linger empty and
# be renamed again to `_1` on the next config change.
for t in text_log trace_log query_log asynchronous_metric_log metric_log \
         part_log query_views_log query_metric_log processors_profile_log; do
  docker exec tms-clickhouse clickhouse-client -q "DROP TABLE IF EXISTS system.${t}_0 SYNC"
done
```

Dropping these is safe: they are ClickHouse's own telemetry, not transaction
data, and nothing in TMS reads them.

If instead you are fixing retention IN PLACE, without mounting the config or
restarting, the order matters:

```bash
# TRUNCATE FIRST, then set the TTL.
for t in text_log trace_log query_log asynchronous_metric_log metric_log \
         part_log query_views_log query_metric_log processors_profile_log; do
  docker exec tms-clickhouse clickhouse-client -q "TRUNCATE TABLE system.$t"
  docker exec tms-clickhouse clickhouse-client -q \
    "ALTER TABLE system.$t MODIFY TTL event_date + INTERVAL 7 DAY"
done
```

`MODIFY TTL` on a populated table makes ClickHouse materialize the TTL across
every existing part; on a 14 GB table that mutation exceeded the container's
4 GB `mem_limit` and failed partway (`Code: 241`). Truncating first makes the
same operation instant. Note that an in-place `MODIFY TTL` also stops the
rename from happening later, since the table then matches the config.

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
- `.env.<TMS_ENV>` (e.g. `.env.mainnet`, `.env.preprod`, `.env.preview`): per-network overrides; applied on top of `.env`. Defaults to `.env.preprod` when `TMS_ENV` is unset.
- Shell environment variables override both files.

| Variable | Default | Description |
|---|---|---|
| `CARDANO_NETWORK` | `mainnet` | `mainnet`, `preprod`, or `preview`. The bundled per-network templates (`.env.mainnet.example`, `.env.preprod.example`, `.env.preview.example`, copied to `.env.mainnet` / `.env.preprod` / `.env.preview`) set this to their network; with no per-network file the built-in default is mainnet |
| `OGMIOS_WS_URL` | `ws://localhost:1337` | Ogmios WebSocket endpoint |
| `POSTGRES_PASSWORD` | dev default | The app refuses to start on the well-known dev default unless `TMS_ALLOW_DEV_MODE=1`. Set it for any real deployment, in `.env` (it is named under the compose `environment:` block, so a per-network file cannot override it) |
| `CLICKHOUSE_PASSWORD` | _(empty)_ | The app refuses to start with an empty value unless `TMS_ALLOW_DEV_MODE=1`. Set it in `.env`. The official image re-applies it on EVERY container start (it writes `users.d/default-user.xml` from the variable), so setting it after the stack is already running just needs `docker compose up -d clickhouse` to recreate the container. Do not recreate the volume for this: that destroys `tms_analytics` and `tms_clustering`, and it is not necessary, the existing data is preserved across the password change |
| `POSTGRES_MEM_LIMIT` | `1g` | Compose `mem_limit` for the `postgres` container |
| `CLICKHOUSE_MEM_LIMIT` | `4g` | Compose `mem_limit` for the `clickhouse` container. The default is tight at mainnet volume: this deployment reached 3.0 GiB after 15 days, and a `MODIFY TTL` has already failed here with Code 241. Raise it before a projection-changing upgrade or a large retention change |
| `APP_MEM_LIMIT` | `2g` | Compose `mem_limit` for the `app` container |
| `CLUSTERING_MEM_LIMIT` | `3g` | Compose `mem_limit` for the `clustering` sidecar container |
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
| `GROUPED_ALERTS_MAX_CONTRACTS` | `5000` | Cap on the contracts fetched for the contract-grouped alerts view. That list paginates in Python (a winning clustering verdict can move a transaction between groups, which SQL LIMIT/OFFSET cannot express), so the fetch is bounded. Hitting the cap is logged at WARNING and means some contracts are absent from the grouped view; raise it rather than leaving it truncated |
| `DATUM_DECODE_MAX_NODES` | `20000` | Node budget for decoding one Plutus datum into the tree shown on the transaction detail view. A datum is attacker-controlled data and the depth cap alone bounds only depth, so a flat list of a million entries would still build a million response nodes. Exceeding it marks the result truncated rather than serving a partial tree as if it were complete |
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
| `CIRCULAR_HISTORY_MATERIALIZE` | `true` | Rewrite the circular history columns into existing parts at boot; see [Circular history columns](#circular-history-columns) |
| `CYCLE_MAX_FANOUT` | `50` | Maximum addresses tracked per BFS hop |
| `SANDWICH_SIMPLIFIED_ENABLED` | `true` | Enable structural sandwich pattern detection |
| `BASELINE_MIN_SAMPLES` | `200` | Minimum samples before per-entity baseline is valid |
| `SMTP_ENABLED` | `true` | Send magic-link emails over SMTP; `false` logs the link instead |
| `SMTP_HOST` / `SMTP_PORT` | `mailpit` / `1025` (compose; code default `localhost` / `1025`) | SMTP relay. The compose default is the bundled Mailpit catch-all, see "Magic-link email in production" below |
| `SMTP_FROM_EMAIL` | `noreply@tms.local` | Sender address (code default; `.env.example` ships `noreply@example.com`). Not validated, but set a real domain in production: special-use domains like `.local`/`.test` are rejected by many receivers |
| `APP_BASE_URL` | `http://localhost:8000` | Base URL baked into emailed magic links. Must be the public dashboard URL in production or links will not resolve. Settable per-network (`.env.<name>`) as well as in `.env` |
| `MAGIC_LINK_TTL_MINUTES` | `15` | Magic-link token lifetime |
| `MAGIC_LINK_PER_EMAIL_LIMIT` | `5` | Link requests per address per window (silent throttle, always on) |
| `RAW_STORE_ENABLED` | `true` | Write raw Ogmios payloads to filesystem |
| `RAW_STORE_PATH` | `./data/raw` | Root path for the Data Lake |
| `LIFECYCLE_PENDING_TTL_SECONDS` | `7200` | After this time a PENDING tx is marked DROPPED |
| `HOUSEKEEPING_INTERVAL_SECONDS` | `30` | Tick of the stale-PENDING sweep, retention, and auth purge; runs whether or not `ANALYSIS_ENGINE_ENABLED` is set |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Additional configuration reference

The table above covers the variables an operator changes most often. The
groups below document the rest of the settings surface (defaults shown are
the code defaults from `app/config.py`; Docker Compose overrides some of
them). Most never need changing, but they are here so nothing is a black box.

**Data retention.** Every retention window except `NOTIFY_DEDUP_RETENTION_DAYS`
defaults to `0`, meaning keep forever. Set a positive day count to prune. The
retention sweep runs on the housekeeping tick.

| Variable | Default | Description |
|---|---|---|
| `CH_RETENTION_DAYS_TRANSACTIONS` | `0` | Prune ClickHouse `transactions` rows older than N days |
| `CH_RETENTION_DAYS_IO` | `0` | Prune `transaction_inputs` / `transaction_outputs` / `address_transactions` older than N days |
| `CH_RETENTION_DAYS_FEATURES` | `0` | Prune the analysis feature tables older than N days |
| `LIFECYCLE_RETENTION_DAYS` | `0` | Prune `DROPPED` / `ROLLED_BACK` Postgres lifecycle rows older than N days. `CONFIRMED` rows are never pruned: they are the canonical lifecycle record |
| `MEMPOOL_COLLISION_RETENTION_DAYS` | `0` | Prune mempool-collision bookkeeping older than N days |
| `RAW_STORE_RETENTION_DAYS` | `0` | Prune raw Data-Lake blobs older than N days. Skipped with a logged warning while `RAW_DATA_MAX_BYTES > 0`: capping the ClickHouse payload makes the raw store the only full copy, so it must not also be pruned. Measured on mainnet over 15 days: 196 MiB/day of disk blocks (about 5.7 GiB per 30 days) across roughly 46,000 files/day, so plan for inodes as well as bytes |
| `RETENTION_SWEEP_INTERVAL_HOURS` | `24` | How often the retention sweep runs |
| `NOTIFY_DEDUP_RETENTION_DAYS` | `30` | Prune the notification dedup ledger older than N days |

**Detection toggles.** Every scorer ships enabled. Disable one only to
isolate a noisy class during tuning; a disabled scorer cannot miss its
attack. `FAKE_TOKEN_TESTNET_MODE` MUST stay `false` in production: it
force-enables the mainnet token registry on testnets and floods Moderate with
false positives.

| Variable | Default | Description |
|---|---|---|
| `SCORER_PHISHING_ENABLED` ... `SCORER_CIRCULAR_ENABLED` | `true` | Per-class kill switches: `SCORER_{PHISHING,TOKEN_DUST,LARGE_VALUE,LARGE_DATUM,MULTIPLE_SAT,FAKE_TOKEN,FRONT_RUNNING,SANDWICH,CIRCULAR}_ENABLED` |
| `COLLISION_DETECTION_ENABLED` | `true` | Enable mempool-collision (front-running precursor) detection |
| `FAKE_TOKEN_TESTNET_MODE` | `false` | Testnet-only harness flag; keep `false` in production (see warning above) |

**Clustering sidecar (host side).** See the [Clustering module](README.md#clustering-module-optional) section; these are the host-side knobs.

| Variable | Default | Description |
|---|---|---|
| `CLUSTERING_ENABLED` | `false` | Wire in the `/api/v1/clustering/*` proxy and merge `contract_anomaly` verdicts |
| `CLUSTERING_DB` | `tms_clustering` | ClickHouse database holding the sidecar's state |
| `CLUSTERING_SIDECAR_URL` | `http://clustering:8000` | Sidecar base URL the proxy forwards to |
| `CLUSTERING_SIDECAR_API_KEY` | _(empty)_ | Key forwarded to the sidecar as `X-API-Key` when it requires auth |
| `CLUSTERING_HISTORY_SOURCE` | _(empty)_ | Optional pre-deployment history backfill for watched contracts: `blockfrost` or `kupo`; see `services/clustering/docs/operations.md` |
| `CLUSTERING_HISTORY_MAX_TXS` | `500` | Per-contract history depth (override per contract in the onboarding form) |
| `CLUSTERING_HISTORY_MAX_TXS_CEILING` | `5000` | Clamp on per-contract history overrides |
| `CLUSTERING_HOST_API_URL` / `CLUSTERING_HOST_API_KEY` | `http://app:8000` / _(empty)_ | kupo flavor only: how the sidecar reaches this app's `/api/v1/backfill` |
| `CLUSTERING_HOST_API_TIMEOUT_SECONDS` | `30` | kupo flavor only: per-request ceiling on the sidecar's trigger/status calls to this app |
| `CLUSTERING_HISTORY_MIN_HOST_TXS` | `0` | Skip the backfill when the host already holds at least N rows for the target (its recent sample is a sufficient fit baseline). `0` = always top up |
| `CLUSTERING_CHAIN_SOURCE` | _(empty)_ | Sidecar chain source for the backfill (`blockfrost`); the network comes from `CARDANO_NETWORK` |

**Clustering sidecar (module side).** Set these when the sidecar container is
reachable beyond the compose network. It binds to loopback in the bundled
compose file and its API is unauthenticated by default, so a deployment that
publishes its port **must** set the first two.

| Variable | Default | Description |
|---|---|---|
| `CLUSTERING_API_KEY` | _(empty)_ | Shared key; the host proxy forwards it as `X-API-Key` and the sidecar requires it |
| `CLUSTERING_REQUIRE_AUTH` | _(unset)_ | `1` makes the sidecar refuse to start unless `CLUSTERING_API_KEY` and `MODEL_SIGNING_KEYS` are both set. Turns a missing key into a boot failure rather than a silently open API |
| `CLUSTERING_CORS_ORIGINS` | _(empty)_ | Allowed browser origins for direct sidecar access; leave empty when it sits behind the host proxy |
| `CLUSTERING_DEFAULT_TARGET_TXS` | `5000` | Per-contract "latest N to cluster on" when the operator names none at onboarding |
| `CLUSTERING_MIN_TARGET_TXS` | `200` | Recall floor a named N is clamped up to; below it the outlier baseline is too thin |
| `CLUSTERING_WINDOW_TXS` | `50000` | Window and fit-memory ceiling that caps both of the above |

**Ingestion resilience: raw-store fallback and analysis deferral.** When a
ClickHouse write fails, the transaction is parked and retried from the raw
store rather than dropped; when scoring inputs are not yet available, the
analysis is deferred and retried.

| Variable | Default | Description |
|---|---|---|
| `RAW_FALLBACK_ENABLED` | `true` | Retry failed warehouse writes from the raw store |
| `RAW_FALLBACK_MAX_ATTEMPTS` | `3` | Counted fallback attempts per row; after the budget the tx is scored anyway, degraded, with a `raw_data_unavailable` evidence marker, so a lost blob cannot park it in the pending queue forever |
| `RAW_DATA_MAX_BYTES` | `0` | Per-transaction cap on the serialized payload stored in the ClickHouse `raw_data` column; `0` = store the full payload. Above the cap an **empty** string is written with `raw_data_truncated = 1`, never a partial prefix, and the engine reads the full payload back from the raw store instead. This is not a raw-store size cap: setting it makes the raw store load-bearing, which is why it blocks `RAW_STORE_RETENTION_DAYS` |
| `ANALYSIS_DEFER_ENABLED` | `true` | Defer + retry scoring when enrichment inputs are missing |
| `ANALYSIS_DEFER_MAX_ATTEMPTS` | `3` | Deferred-scoring attempts before the class is persisted as not-applicable |
| `ANALYSIS_DEFER_RETRY_SECONDS` | `30` | Spacing between deferred-scoring attempts |
| `ROLLBACK_CLEANUP_ENABLED` | `true` | On a chain rollback, delete ClickHouse rows for transactions past the rollback point, so orphaned-fork data cannot feed scorers or API reads. `archived_alerts` is exempt: it is admin curation, not chain state |

**Analysis engine internals.** Tuning knobs for the scoring loop; the
defaults suit preprod. `UNANALYZED_FULL_RESCAN_WINDOW_SECONDS` bounds the
periodic full rescan to a recent window, which caps its anti-join cost as
history accumulates (`0` = rescan all history). Treat it as a recall
trade-off, not a routine mainnet setting: the full rescan is the never-skip
guarantee, and the incremental query only looks back
`UNANALYZED_OVERLAP_SECONDS`, so anything still unscored past the window is
never picked up. If ingestion outlives scoring (engine disabled, crash-loop,
multi-day resync) that gap is a permanent detection miss. Bound it only once
the rescan's cost is measured on the deployment, and then pick a window that
comfortably exceeds the longest plausible scoring outage.

| Variable | Default | Description |
|---|---|---|
| `ANALYSIS_MAX_REF_TXS` | `2000` | Cap on reference txs pulled per enrichment fetch |
| `ANALYSIS_ENGINE_MAX_BATCHES_PER_TICK` | `20` | Batches drained per engine tick before yielding |
| `ANALYSIS_ENGINE_DRAIN_SLEEP_SECONDS` | `0.5` | Pause between drained batches |
| `UNANALYZED_OVERLAP_SECONDS` | `120` | Look-back overlap on the incremental unscored-tx query |
| `UNANALYZED_FULL_RESCAN_INTERVAL_SECONDS` | `600` | How often the safety-net full rescan runs |
| `UNANALYZED_FULL_RESCAN_WINDOW_SECONDS` | `0` | Bound the full rescan to the last N seconds; `0` = all history. Narrowing it trades recall for query cost (see the warning above) |

**Baselines.** Per-entity percentile baselines and the token registry.

| Variable | Default | Description |
|---|---|---|
| `BASELINE_BOOTSTRAP_ON_STARTUP` | `true` | Compute baselines at startup instead of waiting for the first recompute |
| `BASELINE_RECOMPUTE_INTERVAL_HOURS` | `24` | Baseline recompute cadence |
| `BASELINE_MAX_SCRIPTS` | `500` | Cap on per-script baselines held |
| `BASELINE_CACHE_TTL_SECONDS` | `3600` | In-process baseline cache TTL |
| `BASELINE_CACHE_MAX_ENTRIES` | `50000` | Baseline cache size cap |
| `TOKEN_REGISTRY_REFRESH_INTERVAL_HOURS` | `24` | Fake-token registry refresh cadence |
| `POLICY_FIRST_SEEN_CACHE_MAX_ENTRIES` | `100000` | Policy first-seen lookup cache size; `0` disables. Only known first-slots are cached and a known slot can only move earlier, so a stale entry can only under-state a policy's age, which fails toward detection. No TTL; overflow clears the cache |

**Database tuning.** Pool sizing, timeouts, and insert-retry backoff. The
defaults match Docker Compose and rarely need changing; raise pool sizes and
timeouts for high-volume mainnet ingestion.

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_POOL_MIN_SIZE` / `POSTGRES_POOL_MAX_SIZE` | `2` / `10` | asyncpg pool bounds |
| `POSTGRES_POOL_MAX_IDLE_SECONDS` | `300` | Idle connection reaping |
| `POSTGRES_STATEMENT_TIMEOUT_SECONDS` | `30` | Per-statement timeout |
| `CLICKHOUSE_CONNECT_TIMEOUT_SECONDS` | `10` | ClickHouse connect timeout |
| `CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS` | `120` | ClickHouse socket send/receive timeout |
| `CLICKHOUSE_INSERT_MAX_RETRIES` | `5` | Batched-insert retries on transient failure |
| `CLICKHOUSE_INSERT_RETRY_BASE_DELAY_SECONDS` / `_MAX_DELAY_SECONDS` | `1` / `30` | Insert-retry exponential backoff bounds |

**Ogmios and pipeline health.** The `PIPELINE_BLOCK_AGE_*` thresholds back
the `pipeline_state` bands reported by `/health/detail` (see "Health checks"
above): older than DEGRADED is `DEGRADED`, older than DOWN is `DOWN`.

| Variable | Default | Description |
|---|---|---|
| `PIPELINE_STARTUP_GRACE_SECONDS` | `60` | Grace before block-age health is enforced after startup |
| `PIPELINE_BLOCK_AGE_DEGRADED_SECONDS` | `120` | Last-block age that flips `pipeline_state` to `DEGRADED` |
| `PIPELINE_BLOCK_AGE_DOWN_SECONDS` | `300` | Last-block age that flips `pipeline_state` to `DOWN` |
| `OGMIOS_RECONNECT_MAX_DELAY` | `60` | Max reconnect backoff delay |
| `OGMIOS_HEARTBEAT_INTERVAL` / `OGMIOS_HEARTBEAT_TIMEOUT` | `30` / `90` | Keepalive ping cadence and dead-connection timeout |
| `OGMIOS_CIRCUIT_BREAKER_THRESHOLD` / `OGMIOS_CIRCUIT_BREAKER_COOLDOWN` | `5` / `120` | Consecutive failures before the breaker opens, and its cooldown |
| `OGMIOS_CIRCUIT_OPEN_POLL_SECONDS` | `10` | Poll cadence while the breaker is open |
| `OGMIOS_SESSION_STABLE_RESET_SECONDS` | `60` | Uptime after which a session counts as stable and the failure count resets |
| `SUPERVISOR_BACKOFF_BASE_SECONDS` / `SUPERVISOR_BACKOFF_MAX_SECONDS` | `5` / `300` | Supervisor restart backoff bounds for the ingestion tasks |
| `SUPERVISOR_STABLE_RESET_SECONDS` | `600` | Run duration after which a supervised task counts as stable and its backoff resets to base. One-off crashes recover fast; a persistently failing task keeps backing off instead of hammering logs and downstream services |
| `OGMIOS_PARSE_EXECUTOR_THRESHOLD_BYTES` | `1048576` | Payload size above which parsing moves to a thread executor |
| `OGMIOS_WS_MAX_FRAME_BYTES` | `67108864` | Max Ogmios WebSocket frame accepted (64 MiB) |

**WebSocket feed.** The handshake rate-limit pair is in the main table above.

| Variable | Default | Description |
|---|---|---|
| `WS_CLIENT_QUEUE_SIZE` | `100` | Per-client outbound broadcast queue depth; when the queue is full the oldest queued event is discarded and the client stays connected (a lagging dashboard wants the newest state) |
| `WS_MAX_CONNECTIONS` | `100` | Max concurrent dashboard WebSocket connections |

**Mempool bookkeeping.**

| Variable | Default | Description |
|---|---|---|
| `MEMPOOL_PENDING_TTL_SECONDS` | `7200` | In-memory mempool-observation lifetime (mirrors `LIFECYCLE_PENDING_TTL_SECONDS`) |
| `MEMPOOL_PRUNE_EVERY_N_TXS` | `100` | Prune the seen-tx set every N observations |
| `MEMPOOL_SEEN_TXS_MAX` | `50000` | Cap on the mempool dedup set |

**Historical backfill (Kupo).** Backs the on-demand `POST /api/v1/backfill`
address history import. `KUPO_SINCE` and `KUPO_MATCH` are Compose-container
settings for the bundled Kupo service, not app config, and they only take
effect from the shell or the top-level `.env`: Compose does not interpolate
from a per-network `.env.<name>`. Requires a reachable Kupo instance (the
`ingestion` profile starts one).

| Variable | Default | Description |
|---|---|---|
| `KUPO_URL` | _(empty)_ | Kupo base URL for the address-to-tx index |
| `KUPO_TIMEOUT_SECONDS` | `30` | Per-request timeout to Kupo |
| `BACKFILL_DEFAULT_MAX_TXS` / `BACKFILL_MAX_TXS_CAP` | `500` / `5000` | Default and hard cap on txs imported per backfill job |
| `BACKFILL_MAX_CONCURRENT` | `1` | Concurrent backfill jobs |
| `BACKFILL_TIMEOUT_SECONDS` | `3600` | Per-job wall-clock timeout |
| `BACKFILL_JOB_RETENTION` | `100` | Completed backfill-job records kept for status polling |

**Notifications delivery.** Channel enable flags and delivery tuning; the
webhook signing secret and internal-egress flag are in the Webhook section
below.

| Variable | Default | Description |
|---|---|---|
| `EMAIL_NOTIFY_ENABLED` / `WEBHOOK_NOTIFY_ENABLED` | `true` | Master enable per channel (routing matrix is set via the notifications config API) |
| `NOTIFY_TOP_FEATURES` | `5` | Top contributing features included in an alert payload |
| `NOTIFY_SEND_TIMEOUT_SECONDS` | `10` | Per-send timeout |
| `NOTIFY_MAX_CONCURRENT_DELIVERIES` | `8` | Concurrent alert deliveries |
| `NOTIFY_GROUP_WINDOW_MINUTES` | `60` | Alert-grouping window. For attack classes where repeat findings at one script are a single situation (`large_datum` only today), one alert per script per window instead of one per transaction. Escalation to a higher band always breaks through, and suppression expires with the window, so a persistent condition re-alerts hourly. Bounds notifications only: every finding is still recorded and visible in the dashboard. `0` disables grouping. See [docs/ALERTING.md](docs/ALERTING.md#per-group-one-alert-per-script-per-window) |
| `WEBHOOK_TIMEOUT_SECONDS` | `8` | Per-webhook HTTP timeout |
| `WEBHOOK_MAX_RETRIES` | `2` | Extra webhook attempts on 5xx / network error |
| `WEBHOOK_RETRY_BACKOFF_SECONDS` | `1` | Webhook retry backoff base |
| `NOTIFY_REPORT_CHECK_INTERVAL_SECONDS` | `60` | Scheduled digest-report check cadence |
| `NOTIFY_REPORT_TOP_ALERTS` | `10` | Alerts included in a digest report |
| `NOTIFY_CONTRACT_ANOMALY_POLL_SECONDS` | `60` | Poll cadence for the contract-anomaly alert path |
| `NOTIFY_CONTRACT_ANOMALY_MAX_ALERTS_PER_TICK` | `50` | Cap on contract-anomaly alerts emitted per poll |
| `NOTIFY_RETRY_CHECK_INTERVAL_SECONDS` | `60` | Failed-delivery retry sweep cadence (the scorer path's dead letter; see [docs/ALERTING.md](docs/ALERTING.md#the-scorer-path-retries-through-a-dead-letter)) |
| `NOTIFY_RETRY_BACKOFF_SECONDS` | `60` | Base delay before a failed alert is retried; doubles per attempt, capped near an hour |
| `NOTIFY_RETRY_MAX_ATTEMPTS` | `8` | Retries before a failed alert is retired (kept in `failed_notifications`, marked abandoned, logged at ERROR) |
| `NOTIFY_RETRY_MAX_PER_TICK` | `20` | Cap on retries attempted per sweep, pacing a backlog drain |

**Auth and SMTP (advanced).** The common auth variables are in the main
table; these are the rest.

| Variable | Default | Description |
|---|---|---|
| `CSRF_PROTECTION_ENABLED` | `true` | Double-submit CSRF protection on session-cookie routes; leave on |
| `SMTP_TIMEOUT_SECONDS` | `10` | SMTP send timeout |
| `MAGIC_LINK_MAX_REDEMPTIONS` | `3` | Times one magic link can be redeemed before it is consumed |
| `MAGIC_LINK_PER_EMAIL_WINDOW_SECONDS` | `900` | Window for the per-address magic-link request throttle |
| `ADMIN_INVARIANT_LOCK_KEY` | `8737367428` | Postgres advisory lock serialising Admin role changes, so the "cannot remove the last active Admin" guard cannot be raced by two concurrent operations. Must stay constant across deploys and must not collide with `LEADER_LOCK_KEY` |

**Application identity and process.** Cosmetic and developer-only settings.
`UVICORN_RELOAD` must stay `false` outside local development.

| Variable | Default | Description |
|---|---|---|
| `API_TITLE` | `Cardano Transaction Monitoring System` | Title shown in the OpenAPI schema and the `/docs` page |
| `API_VERSION` | `0.1.0` | Version string reported in the OpenAPI schema |
| `TMS_CONFIG_DIR` | _(empty)_ | Override the directory searched for `detection.yaml` and the other config files; empty resolves the project root's `config/` directory |
| `UVICORN_RELOAD` | `false` | Uvicorn's file-watch reloader. Local development only: it adds request latency and spawns a watchdog subprocess, so leave it off in Docker and in production |

**Logging.**

| Variable | Default | Description |
|---|---|---|
| `LOG_FORMAT` | `text` | `text` for human-readable logs, `json` for structured logs a collector can parse |

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

The compose stack can bundle Mailpit as a catch-all SMTP sink for development, opt-in via `--profile mail`: with that profile every email the app sends is captured and viewable at `http://127.0.0.1:8025`, and nothing is forwarded. The default deployment (`--profile app`) does not start Mailpit and the app does not depend on it. Magic-link emails are login credentials, so leaving `SMTP_HOST` at its `mailpit` default in production is wrong either way: with `--profile mail` active, every sign-in link accumulates in an unauthenticated inbox on the host; without it, sends fail (logged, tolerated) and no one can sign in.

For production deployments:

1. Point `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (plus `SMTP_USE_TLS` or `SMTP_USE_STARTTLS`) at your organisation's SMTP provider.
2. Set `APP_BASE_URL` to the public dashboard URL so emailed links resolve.
3. Do not pass `--profile mail` in production: Mailpit is not started by the default `--profile app`, and the app has no dependency on it, so there is nothing to stop. (SMTP failures at runtime are tolerated anyway: logged, silent 200 to the caller.)

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

Delivery semantics: within one delivery, transient failures (5xx, network) are retried a bounded number of times and a 4xx is treated as a permanent client error and not retried in place. Separately, a scorer alert whose every channel failed is dead-lettered and re-attempted by a background sweep with exponential backoff for roughly two hours, so a receiver that was down, or that answered 4xx, may see the same alert again; deduplicate on `tx_hash` if that matters to you. Answer with any 2xx quickly, then process asynchronously.

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

## Upgrading the application

The app image bundles the backend and the built dashboard; databases and
their schemas are separate. Schema changes are applied automatically at
startup (idempotent `CREATE TABLE IF NOT EXISTS` / `ALTER ... IF NOT EXISTS`),
so a routine upgrade is pull-and-restart. Two kinds of step are not automatic,
and they fail in opposite ways:

- The legacy dedup migration below. The startup guard demands it by name, so the
  app refuses to start until it has run. You cannot miss it.
- A **post-upgrade backfill**, for a release that adds a column derived from data
  already on the row. Nothing demands these. The app starts, ingests and scores
  normally while the new column sits empty on historical rows, so the only
  symptom is a dashboard feature that looks broken for everything older than the
  upgrade. Check "Post-upgrade backfills" below before calling an upgrade done.

1. Back up first (see "Backup & restore"): a schema-changing release is the
   moment a rollback path matters most.
2. Rebuild the image: `docker compose build app`, which also rebuilds the
   embedded dashboard. The `app` service is built from this repository (no
   registry image is published), so `docker compose pull app` is a no-op and
   would silently redeploy the old image.
3. Recreate the app container: `docker compose --profile app up -d app`.
   Databases keep running; only the app restarts. Startup applies any additive
   schema changes itself.
4. If the app refuses to start and names `backend/scripts/migrate_dedup_schema.py`, run
   the one-shot migration below, then start again.
5. Run any backfill this release needs (see "Post-upgrade backfills"). The app is
   already serving at this point: a backfill only fills history in, so it is safe
   to run against a live instance and safe to defer to a quieter hour.
6. Verify: `curl -s -H "X-API-Key: $TMS_API_KEY" localhost:8000/health/detail`
   (the endpoint requires an API key) should report `pipeline_state: OK` once
   ingestion catches up from the sync checkpoint (a brief `DEGRADED` while it
   replays the gap is normal).

To roll back, rebuild from the previous commit. There is no image tag to
redeploy: as step 2 says, the `app` service is built from this repository and no
registry image is published, so record `git rev-parse HEAD` as step 0 of every
upgrade and roll back with it:

```bash
git checkout <previous-commit>
docker compose build app
docker compose --profile app up -d app
```

The databases and their volumes are untouched by an app rollback, so an additive
release needs no restore. Additive schema changes are backward compatible with
the prior release; a release that required the dedup migration is not (keep the
`<table>__legacy_<date>` tables until the new version is confirmed healthy).

Two things to know before a rollback that crosses a schema change. Changing
`VITE_NETWORK` needs `docker compose build app` rather than a restart, because
the value is baked into the dashboard bundle at build time. And startup runs
`ALTER TABLE transactions MATERIALIZE PROJECTION`, a part-rewriting mutation over
the whole table, inside the same `CLICKHOUSE_MEM_LIMIT` that has already failed a
`MODIFY TTL` with Code 241 on this deployment; raise the limit before a
projection-changing upgrade and expect list queries to fall back to the base
table while it runs. The first boot of a release that adds the circular history
columns runs another part-rewriting mutation, on `tx_class_scores`: see
[Circular history columns](#circular-history-columns) before that upgrade.

### Circular history columns

The circular scorer reads an origin's earlier cycles through three columns
ClickHouse materializes from the stored evidence: `tx_class_scores.circular_origins`,
`circular_intermediaries` and `circular_first_slot`. The first boot of the release
that adds them rewrites them into the existing parts with one `ALTER TABLE
tx_class_scores MATERIALIZE COLUMN ...` (one mutation per column). No backfill
script is needed: until the rewrite finishes, ClickHouse computes the values from
`evidence` on read, so scores are right from the first boot; each history read
just costs about 2 CPU-s and 600 MiB instead of 0.3 CPU-s and 170 MiB. On a
mainnet-shaped synthetic table (1.53M scores) the rewrite took 1.75 s wall, 2.2
CPU-s and 25 MiB peak. Later boots find no part to rewrite and issue nothing. Two
instances that boot at the same moment can both issue it; the second rewrite is
redundant and short. The columns are additive, so a rollback past this release
needs no restore.

Mutations on a table run in order, so every `tx_class_scores` DELETE (the rollback
purge, `scripts/reset.sh`) waits behind the rewrite and fails while it fails, and
chain sync stalls at the next rollback. The app logs a failing rewrite at ERROR on
every boot. Check before and after the deploy:

```sql
SELECT mutation_id, command, parts_to_do, latest_fail_reason
FROM system.mutations
WHERE table = 'tx_class_scores' AND NOT is_done
```

To get out of a rewrite that keeps failing, fix the cause (usually memory: raise
`CLICKHOUSE_MEM_LIMIT`), or kill it and set `CIRCULAR_HISTORY_MATERIALIZE=false` so
the next boot does not issue it again:

```sql
KILL MUTATION WHERE table = 'tx_class_scores' AND command LIKE '%MATERIALIZE COLUMN circular_%'
```

With the setting off the values stay right, computed on read until those parts
merge, and the history reads cost more. Turn it back on once the cause is fixed.

## Schema migration (dedup-safe v2)

Deployments created before the ReplacingMergeTree schema must run the one-shot migration before the app will start (the startup guard refuses a legacy layout and names the script):

1. Stop ALL app instances sharing the ClickHouse database (preprod and preview).
2. Dry-run: `cd backend && python scripts/migrate_dedup_schema.py` (prints per-table row and duplicate counts).
3. Apply: `python scripts/migrate_dedup_schema.py --apply`
4. Restart the instances. Legacy data is preserved as `<table>__legacy_<date>`; drop those tables manually after a verification window.

### Post-upgrade backfills

A backfill populates a newly added column for rows that were written before the
upgrade. The column itself is created automatically at startup; only the
historical values are missing. Nothing in the app fails or warns while they are
missing, which is why this list exists.

Each script is idempotent and resumable: it guards on the rows it can still
improve, so an interrupted run is resumed by simply running it again, and a
completed one reports nothing pending. All of them are dry-run by default and
write only with `--apply`. Run from `backend/`, and with `-m` (the scripts import
`app.*`, so invoking them by path fails with `ModuleNotFoundError: No module
named 'app'`).

| Release adds | Script | Symptom if skipped |
|---|---|---|
| `tx_class_scores.contract_address` | `scripts.oneoff.backfill_contract_address` | The contract-grouped risk-alerts table shows no group for any transaction scored before the upgrade: every one of them renders as an ungrouped row instead |

```bash
# In the container (WORKDIR is already /app/backend):
docker compose exec app python -m scripts.oneoff.backfill_contract_address
# Dry-run: prints the pending count per chunk and a sample of the derived values.
docker compose exec app python -m scripts.oneoff.backfill_contract_address --apply
```

`--chunk-days` (default 30) sizes each `ALTER TABLE ... UPDATE`, and `--sync`
waits for each mutation before starting the next, which keeps load predictable
on a busy instance. ClickHouse mutations run in the background and do not block
inserts, so ingestion and scoring continue throughout; the write touches only the
one column, and the others are hardlinked.

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
