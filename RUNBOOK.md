# Runbook: Transaction Monitoring System

## Prerequisites

### 1. Cardano node + Ogmios (required)

The TMS connects to a Cardano node through Ogmios, a WebSocket bridge. You must have both running and reachable **before** starting the TMS.

Two options:

- **External infrastructure (recommended for production/staging):** run node + Ogmios separately and point `OGMIOS_WS_URL` at the remote endpoint. The details below describe this path.
- **Bundled local stack (development only):** `docker-compose.yml` includes `cardano-node` and `ogmios` services gated behind the `ingestion` profile. Start with `docker-compose --profile ingestion up`. Requires a populated config directory at `./cardano-config/preprod/` (override with `CARDANO_CONFIG_DIR`) containing `config.json` and `topology.json`, plus ~30 GB disk and a multi-hour initial chain sync. Leave `OGMIOS_WS_URL=ws://localhost:1337` (the default).

| Component | Version | Notes |
|---|---|---|
| cardano-node | 8.x / 9.x | Must be fully synced to the target network |
| Ogmios | v6.x | Must be running alongside the node, accessible over WebSocket |

Ogmios listens on port `1337` by default. Verify it is reachable:

```bash
curl -s --no-buffer -H "Connection: Upgrade" -H "Upgrade: websocket" \
  http://<ogmios-host>:1337/health
```

A healthy Ogmios returns a JSON object with `"networkSynchronization": 1` (or close to it).

### 2. Local machine

- Docker and Docker Compose
- Python 3.12+


## First-time setup

```bash
# 1. Clone the repository
git clone <repository-url>
cd TransactionMonitoringSystem

# 2. Copy and edit the configuration
cp .env.example .env
```

Open `.env` and set the two required values:

```bash
CARDANO_NETWORK=preprod        # or mainnet
OGMIOS_WS_URL=ws://<host>:1337 # address of your Ogmios instance
```

Everything else uses safe defaults. If you want API key authentication, also set:

```bash
API_KEYS=your-key-1,your-key-2
```

Leave `API_KEYS` empty during initial testing; the API runs in open dev mode and logs a warning.


## Starting the system

### Option A: databases in Docker, app on host (recommended for development)

```bash
# Start PostgreSQL and ClickHouse
docker compose up -d

# Wait for containers to be healthy
docker compose ps

# Install Python dependencies (first time only)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Start the application
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Option B: everything in Docker

```bash
docker compose --profile app up -d
```

The app container connects to the databases internally. `OGMIOS_WS_URL` must still point to your external Ogmios host.


## Verifying the system is working

### 1. Health check

```bash
curl http://localhost:8000/health
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
# No key needed if API_KEYS is empty
curl "http://localhost:8000/api/transactions/?limit=5&network=preprod"

# With a key
curl -H "TMS-API-Key: your-key" "http://localhost:8000/api/transactions/?limit=5&network=preprod"
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

The application reconnects to Ogmios automatically on restart using an exponential backoff circuit breaker. After a restart it reads the last saved `sync_checkpoint` from PostgreSQL and resumes from that slot, so no blocks are missed.

```bash
# Restart just the app (databases keep running)
# Ctrl+C the uvicorn process, then:
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```


## Configuration reference

All variables are set in `.env`. The application reads them at startup.

| Variable | Default | Description |
|---|---|---|
| `CARDANO_NETWORK` | `preprod` | `mainnet` or `preprod` |
| `OGMIOS_WS_URL` | `ws://localhost:1337` | Ogmios WebSocket endpoint |
| `API_KEYS` | _(empty)_ | Comma-separated API keys. Empty = open access (dev mode) |
| `RATE_LIMIT_ENABLED` | `true` | Enable per-key sliding-window rate limiting |
| `RATE_LIMIT_REQUESTS` | `60` | Max requests per window per key |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window in seconds |
| `ANALYSIS_ENGINE_ENABLED` | `true` | Run background risk scoring |
| `ANALYSIS_ENGINE_INTERVAL_SECONDS` | `30` | How often the engine polls for unscored transactions |
| `ANALYSIS_ENGINE_BATCH_SIZE` | `100` | Transactions scored per run |
| `ANALYSIS_ENABLED` | `true` | Enable multi-class detection engine |
| `CYCLE_DETECTION_ENABLED` | `true` | Enable transfer graph cycle detection |
| `CYCLE_MAX_HOPS` | `6` | Maximum BFS depth for cycle detection |
| `CYCLE_MAX_FANOUT` | `50` | Maximum addresses tracked per BFS hop |
| `SANDWICH_SIMPLIFIED_ENABLED` | `true` | Enable structural sandwich pattern detection |
| `BASELINE_MIN_SAMPLES` | `200` | Minimum samples before per-entity baseline is valid |
| `RAW_STORE_ENABLED` | `true` | Write raw Ogmios payloads to filesystem |
| `RAW_STORE_PATH` | `./data/raw` | Root path for the Data Lake |
| `LIFECYCLE_PENDING_TTL_SECONDS` | `7200` | After this time a PENDING tx is marked DROPPED |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Database variables (`POSTGRES_*`, `CLICKHOUSE_*`) default to the values used by Docker Compose and rarely need changing.


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
curl http://localhost:8000/health
```

Check the `ogmios` field for `chain_circuit_state` and `mempool_circuit_state`. If `OPEN`, the circuit breaker tripped after repeated failures. It will attempt a probe after a 2-minute cooldown automatically; no manual action needed unless the underlying connectivity problem persists.

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

1. Check `pipeline_state` is `OK` at `/health`
2. Ogmios may still be syncing; `networkSynchronization` in its health endpoint should be `1`
3. On Preprod, blocks arrive roughly every 20 seconds; wait at least one full block cycle
4. Check logs for parser errors: `grep -i error` in the application log

### Reset all data

**Destructive: deletes everything in the databases and the raw data volume.**

```bash
docker compose down -v
docker compose up -d
```

On next startup the application recreates all schemas automatically and begins syncing from the chain tip.


## API quick reference

All endpoints accept `?network=preprod` or `?network=mainnet` (default: `preprod`).
All endpoints require `TMS-API-Key: <key>` header unless `API_KEYS` is empty.

```bash
BASE=http://localhost:8000
KEY="TMS-API-Key: your-key"

# Recent transactions
curl -H "$KEY" "$BASE/api/transactions/?limit=20"

# Single transaction
curl -H "$KEY" "$BASE/api/transactions/<tx_hash>"

# All transactions for an address
curl -H "$KEY" "$BASE/api/transactions/address/<addr>"

# Pending (mempool) transactions
curl -H "$KEY" "$BASE/api/lifecycle?status=PENDING"

# Transaction lifecycle state
curl -H "$KEY" "$BASE/api/lifecycle/<tx_hash>"

# Risk analysis results
curl -H "$KEY" "$BASE/api/analysis/results?risk_band=High&limit=50"

# Analysis result for a single transaction
curl -H "$KEY" "$BASE/api/analysis/results/<tx_hash>"

# Aggregate stats
curl -H "$KEY" "$BASE/api/transactions/stats/summary"
curl -H "$KEY" "$BASE/api/lifecycle/stats/summary"
curl -H "$KEY" "$BASE/api/analysis/stats"

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
