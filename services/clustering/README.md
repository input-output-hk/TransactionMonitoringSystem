# Clustering module (contract-anomaly detection)

A TMS detection module that clusters a watched contract's transactions and
flags outliers, surfacing them as the `contract_anomaly` attack class. It runs
as a dedicated service alongside the API and chain-sync workers (the
`clustering` service in the repo-root `docker-compose.yml`, enabled with
`--profile clustering` and the `CLUSTERING_ENABLED` flag).

Where the nine core scorers in `backend/` judge each transaction on its own,
this module judges a transaction *relative to its contract's population*: it
fits per-contract DBSCAN clusters and an anomaly ensemble (Isolation Forest +
Local Outlier Factor + DBSCAN-noise), then classifies new transactions against
the frozen model. Its verdicts are published to the `tms_clustering` ClickHouse
database and merged into `/api/analysis/results` so they appear in the dashboard
like any other attack class. The Validators UI for managing watched contracts
and exploring their clusters lives in the main SPA (`frontend/src/pages` /
`components/clustering`, reached through the API's `/api/clustering` proxy).

## How it runs

In its default mode (`CHAIN_SOURCE=host_ch`) the module reads each watched
contract's transactions from the system's `tms_analytics` ClickHouse database (the
same chain data the core scorers use, via `HostBackedRepo`) and writes its own state
to the sibling `tms_clustering` database on the same ClickHouse server, so no chain
data is duplicated. A scheduler polls the watchlist and onboards/classifies contracts
automatically as new transactions are ingested, then publishes per-transaction
verdicts to `tms_clustering.tx_contract_anomaly`. `app.cli migrate` creates the
`tms_clustering` schema idempotently on startup.

Setting `CHAIN_SOURCE=blockfrost` selects an alternative source that downloads an
arbitrary address's transaction history over HTTP (blockfrost.io) into
`tms_clustering` on demand, for clustering contracts the host has not ingested. In
that mode the automatic feed is off (onboarding is manual) and the downloaded
transactions are stored in `tms_clustering`; the clustering/anomaly analysis is
identical to `host_ch`.

The detection algorithms, feature engineering, online-classification design, and
data model are documented in [docs/](docs/).

## Layout

```
backend/            FastAPI + Typer service (app/, tests/, Dockerfile, pyproject)
  app/sources/host_ch/   default source: read the system's own chain data (no external provider)
  app/blockfrost/        alt source (CHAIN_SOURCE=blockfrost): download address history over HTTP
  app/storage/clickhouse/host_backed.py   cross-database feature reads
  app/service/publish.py + scheduler.py   verdict projection + the automatic feed
clickhouse/init/    idempotent schema (001…009_contract_anomaly), applied by migrate
docs/               detection algorithms, data model, and design reference
```

## Dev

```bash
# Build + run as part of the TMS stack:
docker compose --profile clustering up -d --build clustering

# Tests / lint (hermetic; no ClickHouse or network needed). Run locally with uv:
# the shipped runtime image is the slim production stage and contains neither the
# dev tooling (pytest/ruff/mypy) nor the tests/ directory.
cd services/clustering/backend
uv sync --extra dev
uv run pytest -q
uv run ruff check app tests
```
