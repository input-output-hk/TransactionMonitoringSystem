# Docker Database Setup

Databases run in Docker. The app can run on the host (development) or as a container (production).

**Scope: local development.** The credentials and defaults below are the ones the
stack ships with, and the application refuses to start on them unless
`TMS_ALLOW_DEV_MODE=1` is set. That switch is what gates the credential guards,
not whether API keys are configured (an empty `API_KEYS` is itself a third
refusal under the same switch). For a production or mainnet deployment follow
[docs/MAINNET-DEPLOYMENT.md](docs/MAINNET-DEPLOYMENT.md) instead.

## Quick Commands

```bash
docker-compose up -d           # start databases
docker-compose down            # stop
docker-compose down -v         # stop + delete all data
docker-compose ps              # status
docker-compose logs -f         # logs
```

App + databases together:
```bash
docker-compose --profile app up -d
```

## Services

| Service | Container | Profile | Purpose |
|---|---|---|---|
| `postgres` | `tms-postgres` | default | Operational database (lifecycle, auth, audit) |
| `clickhouse` | `tms-clickhouse` | default | Analytics warehouse |
| `mailpit` | `tms-mailpit` | `mail` | Opt-in dev/demo SMTP sink for magic-link emails (SMTP `:1025`, web UI `:8025`); started only with `--profile mail` |
| `app` | `tms-app` | `app` | FastAPI application (host-run by default in development) |
| `clustering` | `clustering-sidecar` | `clustering` | Optional contract-anomaly sidecar (per-contract DBSCAN + anomaly ensemble); publishes the `contract_anomaly` verdict |
| `cardano-node` | `tms-cardano-node` | `ingestion` | Full node, pinned to `11.0.1` (van Rossem PV11) |
| `ogmios` | `tms-ogmios` | `ingestion` | WebSocket bridge `:1337`, pinned to `v6.14.0` |
| `kupo` | `tms-kupo` | `ingestion` | Address→tx index (`:1442`) for on-demand historical backfill (`POST /api/v1/backfill`) |

## Connection Details

Both sets of credentials below are the local development defaults, and both are
values the startup guards reject: the app refuses to boot on the well-known
`POSTGRES_PASSWORD` default or an empty `CLICKHOUSE_PASSWORD` unless
`TMS_ALLOW_DEV_MODE=1`. Set real values in `.env` before deploying anywhere.

### PostgreSQL
- Host: `localhost:5433` (host port, mapped from container :5432)
- Database: `tms_db`, User: `tms_user`, Password: the dev default in `.env.example`

```bash
docker exec -it tms-postgres sh -c \
  'exec psql -U "${POSTGRES_USER:-tms_user}" -d "${POSTGRES_DB:-tms_db}"'
# or via scripts
./scripts/db.sh psql
```

### ClickHouse
- Native: `localhost:9000`, HTTP: `localhost:8123`
- Database: `tms_analytics`, User: `default`, no password in dev

```bash
# clickhouse-client picks up CLICKHOUSE_PASSWORD from the container's own
# environment, so this works with or without a password set and no secret
# crosses from the host. Do not add `-e CLICKHOUSE_PASSWORD`: with the variable
# unset in your shell that flag strips the container's value and you get
# `Code: 516 Authentication failed`.
docker exec -it tms-clickhouse sh -c \
  'exec clickhouse-client --user "${CLICKHOUSE_USER:-default}"'
curl http://localhost:8123/ping
# or via scripts (same idiom, plus the psql equivalent)
./scripts/db.sh clickhouse
```

## Troubleshooting

**Port conflict** (host ports 5433 / 9000 / 8123 / 1025 / 8025 already in use): change the port mapping in `docker-compose.yml` and update `.env` (`POSTGRES_PORT` defaults to host 5433, mapped to container 5432).

**Container won't start**:
```bash
docker-compose logs postgres
docker-compose logs clickhouse
```

**Reset everything**:
```bash
docker-compose down -v && docker-compose up -d
```

To reset a single network rather than all data, use `./scripts/reset.sh` (see [RUNBOOK.md §Reset all data](RUNBOOK.md#reset-all-data)).
