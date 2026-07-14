# Cardano Transaction Monitoring System

Real-time transaction monitoring system for the Cardano blockchain. Ingests blocks and mempool events via Ogmios, tracks full transaction lifecycle (PENDING → CONFIRMED → ROLLED_BACK), scores transactions against 9 Polimi attack classes (Token Dust, Large Value, Large Datum, Multiple Satisfaction, Front-Running, Sandwich, Circular Transfers, Fake Token, Phishing), and exposes a REST API and live WebSocket feed. Access is gated two ways: programmatic clients authenticate with a `X-API-Key` header, while the operator dashboard uses magic-link email login with role-based accounts (Admin / Reviewer) backed by PostgreSQL.

**For step-by-step setup and operations see [RUNBOOK.md](RUNBOOK.md).**

## Prerequisites

- A running Cardano node + Ogmios v6: either external infrastructure (see [RUNBOOK.md §Prerequisites](RUNBOOK.md#prerequisites)) or the bundled local stack (see [Ingestion modes](#ingestion-modes) below)
- Python 3.13+ (managed via [uv](https://docs.astral.sh/uv/); the clustering sidecar uses the same version)
- Docker and Docker Compose

## Ingestion modes

The `cardano-node` and `ogmios` services in `docker-compose.yml` are gated behind the `ingestion` Compose profile, so you can choose where transactions come from:

| Mode | How to start | When to use |
|---|---|---|
| **Remote Ogmios** | Set `OGMIOS_WS_URL=ws://<host>:1337` in `.env`, then `docker compose --profile app up` | Production, staging, or any environment with shared node infrastructure |
| **Local full stack** | Place node config at `./cardano-config/preprod/` (or set `CARDANO_CONFIG_DIR`), keep default `OGMIOS_WS_URL=ws://localhost:1337`, then `docker compose --profile app --profile ingestion up` | Self-contained local development; requires ~30 GB disk and initial chain sync |
| **No ingestion** | `docker compose --profile app up` with no reachable Ogmios | API/dashboard work against whatever has already been ingested (an empty database on first run) |

`CARDANO_CONFIG_DIR` must contain `config.json` and `topology.json` for the target network. Defaults to `./cardano-config/preprod`.

## Setup

The supported way to run the full stack is Docker Compose. The application (API + dashboard) is gated behind the `app` Compose profile.

```bash
# 1. Clone and enter the repository
git clone <repository-url>
cd <repository-directory>

# 2. Configure
cp .env.example .env
# Edit .env: set API_KEYS (or TMS_ALLOW_DEV_MODE=1 for local-only open access)
# and OGMIOS_WS_URL to point at your Ogmios v6 endpoint.

# 3. Start the full stack (databases + API + dashboard)
docker compose --profile app up -d
# API on http://localhost:8000, dashboard at http://localhost:8000/

# 4. Bootstrap the first dashboard account (one-time).
# Creates an Admin and emails a magic-link; --no-email prints the link to stdout.
docker compose exec app python -m app.cli create-admin you@example.com "Your Name" --no-email
```

With no per-network override file present, the app monitors **mainnet** using its built-in defaults. To monitor a testnet, create the per-network file from the tracked template and select it with `TMS_ENV`:

```bash
cp .env.preprod.example .env.preprod                  # then edit OGMIOS_WS_URL
TMS_ENV=preprod docker compose --profile app up -d    # preprod, API on 8000

cp .env.preview.example .env.preview                  # then edit OGMIOS_WS_URL
TMS_ENV=preview docker compose --profile app up -d    # preview, API on 8001
```

`.env.preprod` and `.env.preview` are gitignored; only the `.example` templates are tracked. Each sets `CARDANO_NETWORK`, `OGMIOS_WS_URL`, and `API_PORT` for its network. To run the bundled Cardano node and Ogmios as well, add the `ingestion` profile (see [Ingestion modes](#ingestion-modes)).

### Local development (host Python)

For backend work you can run the API on the host against the Compose databases:

```bash
uv sync                                            # .venv from uv.lock (Python 3.13)
docker compose up -d postgres clickhouse mailpit   # databases only
cd backend && uv run python run.py                 # or ./scripts/start.sh
```

`run.py` binds uvicorn to `settings.API_PORT`. A direct `uvicorn app.main:app` invocation needs `--port` on the CLI, because uvicorn does not read pydantic settings.

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

`CLUSTERING_ENABLED=true` makes the app wire in the `/api/v1/clustering/*`
reverse-proxy (for the Validators / cluster-graph UI) and merge the module's
`contract_anomaly` verdict into `/api/v1/analysis/*`. Without the profile (or with
`CLUSTERING_ENABLED` unset) the system runs as the nine-class engine, unchanged.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#clustering-module-contract_anomaly)
for how it integrates and [services/clustering/README.md](services/clustering/README.md)
for the module itself.

## Configuration

Configuration is layered across files:

- `.env`: values shared across every network (DB ports, API keys, rate limits, logging, Ogmios tuning).
- `.env.preprod`, `.env.preview`, `.env.<name>`: per-network overrides, each setting `CARDANO_NETWORK`, `OGMIOS_WS_URL`, and `API_PORT`. These files are gitignored; copy the tracked `.env.preprod.example` / `.env.preview.example` templates to create them.

Selection is via `TMS_ENV=<name>` at launch. The selected per-network file is layered on top of `.env` if it exists; with none present, the app uses its built-in defaults (mainnet). Shell env vars override both files.

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

Postgres (`POSTGRES_*`), ClickHouse (`CLICKHOUSE_*`), the full SMTP/session/magic-link set, and trusted-proxy / CORS settings are documented in `.env.example` and the [RUNBOOK configuration reference](RUNBOOK.md#configuration-reference). Database defaults match the Docker Compose setup and rarely need changing. Two first-run notes: when `API_KEYS` is set, the app also refuses to start if `CORS_ALLOW_ORIGINS` is empty or `*` (set it to your dashboard origin); and `TRUSTED_PROXY_ENABLED` ships as `true` in `.env.example` while its code default is `false`.

## API

The API accepts two authentication methods. Programmatic clients send a `X-API-Key` header. Browser/dashboard clients authenticate with a magic-link session cookie obtained via `/api/v1/auth/*` (no API key needed). For local dev without a key set, boot with both `API_KEYS=` (empty) and `TMS_ALLOW_DEV_MODE=1`; requests are then accepted without either credential.
All data endpoints accept an optional `network` query parameter (`mainnet`, `preprod`, or `preview`); defaults to the instance's `CARDANO_NETWORK`.

Interactive docs: `http://localhost:8000/docs`

### Transactions
| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/transactions` | List transactions (params: `limit`, `before`, `address`) |
| GET | `/api/v1/transactions/{tx_hash}` | Transaction detail with inputs, outputs, `block_index` |
| GET | `/api/v1/transactions/address/{address}` | Transactions for an address |
| GET | `/api/v1/transactions/stats/summary` | Aggregate stats (count, volume, fees) |
| GET | `/api/v1/transactions/blocks/recent` | Recent blocks derived from the transactions table (params: `limit`) |
| GET | `/api/v1/transactions/stats/throughput` | Recent transactions per minute over a sliding window (params: `window_minutes`) |

### Lifecycle
| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/lifecycle` | List lifecycle records (params: `status`, `limit`, `offset`) |
| GET | `/api/v1/lifecycle/{tx_id}` | Lifecycle state for a single transaction |
| GET | `/api/v1/lifecycle/stats/summary` | Pending count, avg latency, rollback rate |

Lifecycle statuses: `PENDING` (mempool), `CONFIRMED` (in block), `ROLLED_BACK` (chain reorg), `DROPPED` (pending beyond TTL without confirmation).

### Analysis
| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/analysis/results` | Analysis results (params: `risk_band`, `min_score`, `min_corroboration`, `attack_class`, `sort`, `analyzed_from`, `analyzed_to`, `limit`, `offset`). Valid `attack_class` values are the nine stored classes (`token_dust`, `large_value`, `large_datum`, `multiple_sat`, `front_running`, `sandwich`, `circular`, `fake_token`, `phishing`) plus the synthetic `contract_anomaly` (resolved at read time; returns empty when the clustering profile is off). |
| GET | `/api/v1/analysis/results/{tx_hash}` | Analysis result for a single transaction |
| GET | `/api/v1/analysis/stats` | Risk-band distribution and per-class score stats |
| GET | `/api/v1/analysis/stats/timeseries` | Daily High and Critical alert counts (params: `days`) |
| GET | `/api/v1/analysis/baselines/{scope_type}/{scope_id}` | Baseline percentiles for a scope |

### Authentication & Users
| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/auth/request-link` | Request a magic-link login email for an address |
| GET | `/api/v1/auth/verify` | Verify a magic-link token and start a session |
| POST | `/api/v1/auth/logout` | Invalidate the current session |
| GET | `/api/v1/auth/me` | Current authenticated user |
| GET | `/api/v1/users` | List users (Admin) |
| POST | `/api/v1/users` | Invite a user (Admin) |
| DELETE | `/api/v1/users/{user_id}` | Remove a user (Admin) |
| POST | `/api/v1/users/{user_id}/resend-invite` | Resend an invite magic-link (Admin) |

First-admin bootstrap is done from the CLI, not the API: `python -m app.cli create-admin <email> "<name>"` (see [Setup](#setup)).

### Archive (false-positive suppression)
| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/archive` | Archive an analysis result as a known false positive (`tx_hash`, `network`, `note`, `archived_by`) |
| GET | `/api/v1/archive` | List archived (suppressed) results |
| POST | `/api/v1/archive/bulk` | Bulk-import archive entries |
| GET | `/api/v1/archive/export` | Export archived entries as CSV |
| GET | `/api/v1/archive/{tx_hash}` | Single archive entry |
| DELETE | `/api/v1/archive/{tx_hash}` | Restore (un-archive) an entry |

### Notifications config
| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/notifications/config` | Current notification config (channels, band x attack-class trigger matrix, recipients, periodic report); Admin session required |
| PUT | `/api/v1/notifications/config` | Replace the notification config; validated, applied without restart, audit-logged |

### Clustering (optional sidecar proxy)
| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/clustering/{path}` | Same-origin reverse proxy to the clustering sidecar's read API (contracts, clusters, runs); active only when `CLUSTERING_ENABLED=true` |
| POST/PATCH/DELETE | `/api/v1/clustering/{path}` | Proxied sidecar mutations (watch a contract, label, tune); requires an Admin session or API key (Reviewer sessions rejected) and is audit-logged |

### Other
| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/entities/{type}/{id}` | Entity state |
| PUT | `/api/v1/entities/{type}/{id}` | Set entity state |
| GET | `/health` | Minimal unauthenticated liveness probe: `{"status":"healthy"}` |
| GET | `/health/ready` | Unauthenticated readiness probe; returns 503 while the ingestion pipeline is DOWN (the intended load-balancer gate) |
| GET | `/health/detail` | Full pipeline + Ogmios state (requires API key) |
| WS  | `/ws` | Real-time lifecycle events (TX_PENDING, TX_CONFIRMED, TX_ROLLED_BACK) |
| GET | `/` | Operator dashboard (HTML) |

### Example

```bash
# List recent transactions
curl -H "X-API-Key: your-key" "http://localhost:8000/api/v1/transactions?limit=20&network=preprod"

# Check mempool
curl -H "X-API-Key: your-key" "http://localhost:8000/api/v1/lifecycle?status=PENDING"

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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/C4-ARCHITECTURE.md](docs/C4-ARCHITECTURE.md), and [docs/TECHNOLOGY-DECISIONS.md](docs/TECHNOLOGY-DECISIONS.md) for details. The full documentation index is in [docs/README.md](docs/README.md).

## Detection

The analysis engine scores every transaction against nine supervised attack classes, each on a continuous 0-100 risk scale:

| Class | What it flags |
|---|---|
| Token Dust | A script-address UTxO stuffed with many distinct native assets, bloating the Value-field CBOR (a denial-of-service vector) |
| Large Value | A script-address UTxO holding one asset with an astronomically large quantity (CBOR magnitude bloat) |
| Large Datum | Oversized inline datums used to bloat or grief |
| Multiple Satisfaction | One payment reused to satisfy several script conditions (double satisfaction) |
| Front-Running | A transaction racing ahead of a victim's pending transaction |
| Sandwich | Bracketing a victim transaction with a before-and-after pair |
| Circular Transfers | Funds cycling through a ring of addresses |
| Fake Token | Tokens impersonating a known policy or asset |
| Phishing | Malicious intent signalled through transaction metadata |

Scores roll up into four risk bands (Informational, Moderate, High, Critical). With the optional clustering module enabled, a tenth class, `contract_anomaly`, adds unsupervised per-contract profiling.

The full reference, covering the features extracted per transaction, the scoring framework, and the per-class thresholds, is in [docs/TMS_DETECTION_SPEC.md](docs/TMS_DETECTION_SPEC.md). For how data moves through the pipeline see [docs/DATA-FLOW.md](docs/DATA-FLOW.md), or the plain-English [docs/DATA-FLOW-EXPLAINED.md](docs/DATA-FLOW-EXPLAINED.md).

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

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first: it covers the development setup, how to run the tests, and the rules every change must follow (recall-first detection, no magic numbers, and the documentation style). To report a security issue, see [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party dependency and bundled-data licenses are listed in [docs/LICENSES.md](docs/LICENSES.md).
