# Cardano Transaction Monitoring System

Real-time transaction monitoring system for the Cardano blockchain. Ingests blocks and mempool events via Ogmios, tracks full transaction lifecycle (PENDING → CONFIRMED → ROLLED_BACK), and exposes a REST API and live WebSocket feed.

**For step-by-step setup and operations see [RUNBOOK.md](RUNBOOK.md).**

## Prerequisites

- A running Cardano node + Ogmios v6 (external infrastructure — see [RUNBOOK.md §Prerequisites](RUNBOOK.md#prerequisites))
- Python 3.12+
- Docker and Docker Compose

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
# Edit .env — set OGMIOS_WS_URL, API_KEYS, and network

# 5. Start databases
docker-compose up -d

# 6. Start server
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or use the helper script: `./scripts/start.sh`

## Configuration

Key variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `CARDANO_NETWORK` | `preprod` | `mainnet` or `preprod` |
| `OGMIOS_WS_URL` | `ws://localhost:1337` | Ogmios WebSocket endpoint |
| `API_KEYS` | _(empty)_ | Comma-separated API keys. Empty = open (dev mode) |
| `RATE_LIMIT_ENABLED` | `true` | Enable per-key rate limiting |
| `RATE_LIMIT_REQUESTS` | `60` | Max requests per window |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding window duration |
| `ANALYSIS_ENGINE_ENABLED` | `true` | Run background analysis engine |
| `ANALYSIS_ENGINE_INTERVAL_SECONDS` | `30` | Analysis poll interval |
| `LOG_LEVEL` | `INFO` | Log verbosity |

Database defaults match the Docker Compose setup and rarely need changing. See `.env.example` for the full list.

## API

All endpoints require `TMS-API-Key` header (unless `API_KEYS` is empty).
All endpoints accept an optional `network` query parameter (`mainnet` or `preprod`, default: `preprod`).

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

Lifecycle statuses: `PENDING` (mempool), `CONFIRMED` (in block), `ROLLED_BACK` (chain reorg).

### Analysis
| Method | Path | Description |
|---|---|---|
| GET | `/api/analysis/results` | Analysis results (params: `risk_level`, `limit`, `offset`) |
| GET | `/api/analysis/results/{tx_hash}` | Analysis result for a single transaction |
| GET | `/api/analysis/stats` | Risk distribution, anomaly count, cluster count |

### Other
| Method | Path | Description |
|---|---|---|
| GET | `/api/entities/{type}/{id}` | Entity state |
| PUT | `/api/entities/{type}/{id}` | Set entity state |
| GET | `/health` | Service health + Ogmios connection status |
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

- **ChainSync** — streams new blocks via Ogmios, enriches transactions with cached UTxO input data, persists to ClickHouse, updates lifecycle to CONFIRMED
- **Mempool Monitor** — polls `LocalTxMonitor` via Ogmios, resolves input UTxOs via `queryLedgerState/utxo`, writes PENDING lifecycle records to PostgreSQL, broadcasts TX_PENDING over WebSocket
- **Analysis Engine** — polls ClickHouse for unscored transactions, applies mock risk scoring (M1), writes results back to ClickHouse

| Store | Purpose |
|---|---|
| ClickHouse | Analytics Warehouse — transactions, inputs, outputs, analysis results |
| PostgreSQL | Lifecycle state, sync checkpoint, entity state, audit logs |
| Filesystem | Data Lake — write-once gzip JSON blobs of raw Ogmios payloads |

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

See [README_DOCKER.md](README_DOCKER.md) for connection details and troubleshooting.

## License

[Apache License 2.0](LICENSE)
