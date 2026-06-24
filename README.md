# Cardano Transaction Monitoring System

Real-time transaction monitoring system for the Cardano blockchain. Ingests blocks and mempool events via Ogmios, tracks full transaction lifecycle (PENDING → CONFIRMED → ROLLED_BACK), scores transactions against 9 Polimi attack classes (Token Dust, Large Value, Large Datum, Multiple Satisfaction, Front-Running, Sandwich, Circular Transfers, Fake Token, Phishing), and exposes a REST API and live WebSocket feed. Access is gated two ways: programmatic clients authenticate with a `TMS-API-Key` header, while the operator dashboard uses magic-link email login with role-based accounts (Admin / Reviewer) backed by PostgreSQL.

**For step-by-step setup and operations see [RUNBOOK.md](RUNBOOK.md).**

## Prerequisites

- A running Cardano node + Ogmios v6 — either external infrastructure (see [RUNBOOK.md §Prerequisites](RUNBOOK.md#prerequisites)) or the bundled local stack (see [Ingestion modes](#ingestion-modes) below)
- Python 3.12+
- Docker and Docker Compose

## Ingestion modes

The `cardano-node` and `ogmios` services in `docker-compose.yml` are gated behind the `ingestion` Compose profile, so you can choose where transactions come from:

| Mode | How to start | When to use |
|---|---|---|
| **Remote Ogmios** | Set `OGMIOS_WS_URL=ws://<host>:1337` in `.env`, then `docker-compose up` | Production, staging, or any environment with shared node infrastructure |
| **Local full stack** | Place node config at `./cardano-config/preprod/` (or set `CARDANO_CONFIG_DIR`), keep default `OGMIOS_WS_URL=ws://localhost:1337`, then `docker-compose --profile ingestion up` | Self-contained local development; requires ~30 GB disk and initial chain sync |
| **No ingestion** | `docker-compose up` with ingestion disabled | Frontend/API work against pre-populated data |

`CARDANO_CONFIG_DIR` must contain `config.json` and `topology.json` for the target network. Defaults to `./cardano-config/preprod`.

## Setup

```bash
# 1. Clone and enter
git clone <repository-url>
cd TransactionMonitoringSystem

# 2. Virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env (shared values: DB ports, log level, API keys).
# Network-specific values live in .env.preprod / .env.preview (already
# tracked as local overrides — each holds its CARDANO_NETWORK,
# OGMIOS_WS_URL and API_PORT).

# 5. Start databases
docker-compose up -d

# 6. Start server (defaults to preprod — API on port 8000)
cd backend
python run.py

# 7. Bootstrap the first dashboard account (one-time; needs PostgreSQL up).
# Creates an Admin and emails a magic-link; --no-email prints the link to stdout
# instead (use when SMTP is not configured yet).
python -m app.cli create-admin you@example.com "Your Name" --no-email

# Switch network at launch via TMS_ENV (loads .env.<name> on top of .env):
#   TMS_ENV=preview python run.py   # preview — API on 8001
#   TMS_ENV=preprod python run.py   # explicit preprod (same as default)
```

`run.py` binds uvicorn to `settings.API_PORT` (set per-network in `.env.<name>`).
If you call `uvicorn app.main:app` directly you'll need `--port` on the CLI
because uvicorn itself does not read pydantic settings.

Or use the helper script: `./scripts/start.sh`

## Clustering module (optional)

A first-party sidecar (`services/clustering/`) adds unsupervised, per-contract
profiling on top of the nine supervised scorers, surfaced as a tenth attack
class, `contract_anomaly`. It is fully opt-in and gated behind the `clustering`
Compose profile:

```bash
# Bring it up alongside the app (shares the ClickHouse server; its state lives
# in the tms_clustering database, chain reads come from tms_analytics):
CLUSTERING_ENABLED=true docker compose --profile app --profile clustering up -d
```

`CLUSTERING_ENABLED=true` makes the app wire in the `/api/clustering/*`
reverse-proxy (for the Validators / cluster-graph UI) and merge the module's
`contract_anomaly` verdict into `/api/analysis/*`. Without the profile (or with
`CLUSTERING_ENABLED` unset) the system runs as the nine-class engine, unchanged.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#clustering-module-contract_anomaly)
for how it integrates and [services/clustering/README.md](services/clustering/README.md)
for the module itself.

## Configuration

Configuration is layered across files:

- `.env` — values shared across every network (DB ports, API keys, rate limits, logging, Ogmios tuning).
- `.env.preprod`, `.env.preview`, `.env.<name>` — per-network overrides. Each file sets `CARDANO_NETWORK`, `OGMIOS_WS_URL`, and `API_PORT`.

Selection is via `TMS_ENV=<name>` at launch; unset defaults to `preprod`. Shell env vars override both files.

**Per-network variables** (defined in `.env.<name>`):

| Variable | Example | Description |
|---|---|---|
| `CARDANO_NETWORK` | `preprod` | `mainnet`, `preprod`, or `preview` |
| `OGMIOS_WS_URL` | `ws://<host>:1337` | Ogmios WebSocket endpoint |
| `API_PORT` | `8000` | Port uvicorn binds to |

**Shared variables** (in `.env`):

| Variable | Default | Description |
|---|---|---|
| `API_KEYS` | _(empty)_ | Comma-separated API keys. Empty = open access; the app refuses to start in that mode unless `TMS_ALLOW_DEV_MODE=1` is also set |
| `RATE_LIMIT_ENABLED` | `true` | Enable per-key rate limiting |
| `RATE_LIMIT_REQUESTS` | `240` | Max requests per window per key/IP |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding window duration |
| `ANALYSIS_ENGINE_ENABLED` | `true` | Run background analysis engine |
| `ANALYSIS_ENGINE_INTERVAL_SECONDS` | `30` | Analysis poll interval |
| `ANALYSIS_ENABLED` | `true` | Enable 9-class detection engine |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `APP_BASE_URL` | `http://localhost:8000` | Base URL embedded in magic-link emails |
| `SESSION_TTL_DAYS` | `7` | Dashboard session lifetime |
| `MAGIC_LINK_TTL_MINUTES` | `15` | Magic-link validity window |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_FROM_EMAIL` | _(compose: mailpit)_ | Outbound mail for magic-link delivery |

Postgres (`POSTGRES_*`), ClickHouse (`CLICKHOUSE_*`), the full SMTP/session/magic-link set, and trusted-proxy / CORS settings are documented in `.env.example` and the [RUNBOOK configuration reference](RUNBOOK.md#configuration-reference). Database defaults match the Docker Compose setup and rarely need changing.

## API

The API accepts two authentication methods. Programmatic clients send a `TMS-API-Key` header. Browser/dashboard clients authenticate with a magic-link session cookie obtained via `/api/auth/*` (no API key needed). For local dev without a key set, boot with both `API_KEYS=` (empty) and `TMS_ALLOW_DEV_MODE=1`; requests are then accepted without either credential.
All data endpoints accept an optional `network` query parameter (`mainnet`, `preprod`, or `preview`); defaults to the instance's `CARDANO_NETWORK`.

Interactive docs: `http://localhost:8000/docs`

### Transactions
| Method | Path | Description |
|---|---|---|
| GET | `/api/transactions/` | List transactions (params: `limit`, `before`, `address`) |
| GET | `/api/transactions/{tx_hash}` | Transaction detail with inputs, outputs, `block_index` |
| GET | `/api/transactions/address/{address}` | Transactions for an address |
| GET | `/api/transactions/stats/summary` | Aggregate stats (count, volume, fees) |

### Lifecycle
| Method | Path | Description |
|---|---|---|
| GET | `/api/lifecycle` | List lifecycle records (params: `status`, `limit`, `offset`) |
| GET | `/api/lifecycle/{tx_id}` | Lifecycle state for a single transaction |
| GET | `/api/lifecycle/stats/summary` | Pending count, avg latency, rollback rate |

Lifecycle statuses: `PENDING` (mempool), `CONFIRMED` (in block), `ROLLED_BACK` (chain reorg), `DROPPED` (pending beyond TTL without confirmation).

### Analysis
| Method | Path | Description |
|---|---|---|
| GET | `/api/analysis/results` | Analysis results (params: `risk_band`, `min_score`, `min_corroboration`, `attack_class`, `sort`, `analyzed_from`, `analyzed_to`, `limit`, `offset`). Valid `attack_class` values are the nine stored classes (`token_dust`, `large_value`, `large_datum`, `multiple_sat`, `front_running`, `sandwich`, `circular`, `fake_token`, `phishing`) plus the synthetic `contract_anomaly` (resolved at read time; returns empty when the clustering profile is off). |
| GET | `/api/analysis/results/{tx_hash}` | Analysis result for a single transaction |
| GET | `/api/analysis/stats` | Risk-band distribution and per-class score stats |

### Authentication & Users
| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/request-link` | Request a magic-link login email for an address |
| GET | `/api/auth/verify` | Verify a magic-link token and start a session |
| POST | `/api/auth/logout` | Invalidate the current session |
| GET | `/api/auth/me` | Current authenticated user |
| GET | `/api/users` | List users (Admin) |
| POST | `/api/users` | Invite a user (Admin) |
| DELETE | `/api/users/{user_id}` | Remove a user (Admin) |
| POST | `/api/users/{user_id}/resend-invite` | Resend an invite magic-link (Admin) |

First-admin bootstrap is done from the CLI, not the API: `python -m app.cli create-admin <email> "<name>"` (see [Setup](#setup) step 7).

### Other
| Method | Path | Description |
|---|---|---|
| GET | `/api/entities/{type}/{id}` | Entity state |
| PUT | `/api/entities/{type}/{id}` | Set entity state |
| GET | `/health` | Minimal unauthenticated liveness probe: `{"status":"healthy"}` |
| GET | `/health/detail` | Full pipeline + Ogmios state (requires API key) |
| WS  | `/ws` | Real-time lifecycle events (TX_PENDING, TX_CONFIRMED, TX_ROLLED_BACK) |
| GET | `/` | Operator dashboard (HTML) |

### Example

```bash
# List recent transactions
curl -H "TMS-API-Key: your-key" "http://localhost:8000/api/transactions/?limit=20&network=preprod"

# Check mempool
curl -H "TMS-API-Key: your-key" "http://localhost:8000/api/lifecycle?status=PENDING"

# Health
curl http://localhost:8000/health
```

## Architecture

Single-process FastAPI application. Three async background tasks run in the same event loop:

- **ChainSync**: streams new blocks via Ogmios, enriches transactions with cached UTxO input data, persists to ClickHouse, updates lifecycle to CONFIRMED
- **Mempool Monitor**: polls `LocalTxMonitor` via Ogmios, resolves input UTxOs via `queryLedgerState/utxo`, writes PENDING lifecycle records to PostgreSQL, broadcasts TX_PENDING over WebSocket
- **Analysis Engine**: polls ClickHouse for unscored transactions, enriches with cross-tx data (mempool collisions, transfer graph cycles, sandwich patterns), runs 9 attack-class scorers (Polimi spec), writes score vectors to ClickHouse

| Store | Purpose |
|---|---|
| ClickHouse | Analytics Warehouse: transactions, inputs, outputs, 9-class score vectors, baselines |
| PostgreSQL | Lifecycle state, sync checkpoint, entity state, mempool collisions, audit logs, and the auth tables (`users`, `magic_link_tokens`, `user_sessions`) |
| Filesystem | Data Lake: write-once gzip JSON blobs of raw Ogmios payloads |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/C4-ARCHITECTURE.md](docs/C4-ARCHITECTURE.md), and [docs/TECHNOLOGY-DECISIONS.md](docs/TECHNOLOGY-DECISIONS.md) for details.

## Database Management

```bash
./scripts/db.sh start       # start containers
./scripts/db.sh stop        # stop containers
./scripts/db.sh logs        # view logs
./scripts/db.sh psql        # PostgreSQL shell
./scripts/db.sh clickhouse  # ClickHouse shell
./scripts/db.sh reset       # reset all data (destructive)
```

To wipe data for a single network rather than everything, prefer the network-aware `./scripts/reset.sh` (honours `TMS_ENV` / `--network`, with `--all` and `--yes` flags). See [README_DOCKER.md](README_DOCKER.md) for connection details and troubleshooting, and [RUNBOOK.md §Reset all data](RUNBOOK.md#reset-all-data) for the full reset procedure.

## License

[Apache License 2.0](LICENSE)
