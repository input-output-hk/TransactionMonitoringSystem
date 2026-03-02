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

**Port conflict** (5432 / 9000 already in use): change port mapping in `docker-compose.yml` and update `.env`.

**Container won't start**:
```bash
docker-compose logs postgres
docker-compose logs clickhouse
```

**Reset everything**:
```bash
docker-compose down -v && docker-compose up -d
```
