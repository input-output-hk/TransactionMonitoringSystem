# Docker Setup

Databases run in Docker. The app and Cardano node can also run containerised via profiles.

## Network Selection

All Cardano services are controlled by a single `NETWORK` variable.
Supported values: `preprod` (default) | `preview` | `mainnet` | `sanchonet`

Set it in your shell or in `.env`:
```bash
export NETWORK=preprod   # or preview / mainnet / sanchonet
```

## First-time Setup (Cardano Node)

### 1. Fetch Ogmios config files

> Required so Ogmios can read the genesis parameters.

```bash
./scripts/fetch-cardano-config.sh    # defaults to preprod
```

### 2. Mithril bootstrap (zero-config)

The `mithril-client` service downloads a certified chain snapshot so cardano-node
starts from near tip (~minutes sync) instead of syncing from genesis (~days).

The genesis verification key and aggregator URL are fetched automatically from the
Mithril GitHub repo for `preprod`, `preview`, and `mainnet`. No manual config needed.

To override (private aggregator or pinned key), set in `.env`:
```
MITHRIL_GENESIS_VERIFICATION_KEY=5b3132372c...5d
MITHRIL_AGGREGATOR_ENDPOINT=https://...
```

## Quick Commands

```bash
# Databases only (no node)
docker compose up -d

# Databases + Cardano node + Ogmios
NETWORK=preprod docker compose --profile ingestion up -d

# Everything (databases + node + app container)
NETWORK=preprod docker compose --profile ingestion --profile app up -d

# Switch to preview
NETWORK=preview docker compose --profile ingestion up -d

docker compose down            # stop all running services
docker compose down -v         # stop + delete named volumes (keeps node chain data)
docker compose ps
docker compose logs -f
```

> **Chain data** lives in `./data/cardano-node/<network>/` (bind-mount, gitignored).
> Switching `NETWORK` automatically uses a separate directory, so chain stores never collide.

## Connection Details

### PostgreSQL
- Host: `localhost:5433`
- Database: `tms_db` / User: `tms_user` / Password: `tms_password`

```bash
docker exec -it tms-postgres psql -U tms_user -d tms_db
./scripts/db.sh psql
```

### ClickHouse
- Native: `localhost:9000`, HTTP: `localhost:8123`
- Database: `tms_analytics` / User: `default` / no password

```bash
docker exec -it tms-clickhouse clickhouse-client
curl http://localhost:8123/ping
./scripts/db.sh clickhouse
```

### Ogmios (when `--profile ingestion` is active)
- WebSocket: `ws://localhost:1337`

## Troubleshooting

**Port conflict** (5432 / 9000 already in use): change the host port mapping in `docker-compose.yml` and update `.env`.

**Cardano node won't start — config not found**:
```bash
NETWORK=preprod ./scripts/fetch-cardano-config.sh
```

**Container won't start**:
```bash
docker compose logs cardano-node
docker compose logs ogmios
docker compose logs postgres
```

**Reset databases** (keeps chain data):
```bash
docker compose down -v && docker compose up -d
```

**Full reset** (including chain data):
```bash
docker compose down -v
rm -rf ./data/cardano-node/
docker compose --profile ingestion up -d
```
