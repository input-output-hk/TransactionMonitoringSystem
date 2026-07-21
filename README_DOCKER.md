# Docker Database Setup

Databases run in Docker. The app can run on the host (development) or as a container (production).

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

### PostgreSQL
- Host: `localhost:5433` (host port, mapped from container :5432)
- Database: `tms_db`, User: `tms_user`, Password: `tms_password`

```bash
docker exec -it tms-postgres psql -U tms_user -d tms_db
# or via scripts
./scripts/db.sh psql
```

### ClickHouse
- Native: `localhost:9000`, HTTP: `localhost:8123`
- Database: `tms_analytics`, User: `default`, no password

```bash
docker exec -it tms-clickhouse clickhouse-client
curl http://localhost:8123/ping
# or via scripts
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
