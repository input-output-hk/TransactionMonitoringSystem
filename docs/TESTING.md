# Testing

This document is the test-suite inventory for the Cardano Transaction
Monitoring System: what each tier covers, how to run it, and how the tiers
map to CI. Counts are current as of commit `54698c8` on `main` and move as
the suite grows; the exact numbers are always whatever CI reports on the
latest commit.

## Test tiers at a glance

| Tier | Location | Count | Services needed | CI job |
|---|---|---|---|---|
| Backend hermetic | `backend/tests/` | 1053 | none (all I/O mocked) | Backend (pytest + recall gate) |
| Recall gate | `backend/tests/analysis/` | subset of the above | none | Backend (run first, on its own) |
| Live-DB tier | `backend/tests/live_db/` | 21 | ClickHouse + Postgres | Live-DB tier (ClickHouse 26.x + Postgres) |
| Performance tier | `backend/tests/perf/` | 3 | ClickHouse (2 of 3) | Performance (separate workflow) |
| Clustering sidecar | `services/clustering/backend/tests/` | 349 | none | Clustering sidecar (pytest) |
| Frontend | `frontend/src/**/*.test.{ts,tsx}` | 40 | none | Frontend (lint + build) |

The default developer command, `pytest tests/` from `backend/`, runs the
1053 hermetic backend tests and nothing that needs a database: the live-DB
and performance tiers are opt-in behind environment flags so a contributor
without Docker still gets a green run.

## Unit and integration testing (backend)

`backend/tests/` is hermetic: every ClickHouse and Postgres call is mocked at
the function boundary, so the suite runs anywhere with no services. It covers
the ingestion pipeline (Ogmios v5/v6 parsing, reconnect/rollback resilience,
mempool handling), the analysis engine and all nine detection scorers, the
notification subsystem, the database query and migration logic, and the API
layer end to end through FastAPI's TestClient (routing, auth, rate limiting,
WebSocket hardening).

```bash
cd backend
uv run pytest tests/ -q                      # full hermetic suite
uv run pytest tests/analysis/ -q             # recall gate only (attack-must-fire)
uv run pytest tests/ -q --cov=app --cov-report=term-missing   # with coverage
```

### The recall gate

`backend/tests/analysis/` is the attack-must-fire tier: the tests that prove
each detection scorer still fires on its real-attack case. CI runs it first
and on its own so a recall regression is unambiguous, and the project's change
rules require every detection-parameter change to keep it green. It is
intended as a required status check on `main`; enabling that branch
protection is a pending repository-admin step.

### Coverage

CI measures line coverage on the full backend suite (`--cov=app`) and reports
it in the job summary; there is no enforced threshold yet (report-only). At
commit `54698c8` backend coverage is 76% and the clustering sidecar is 82%.

## Live-DB integration tier

`backend/tests/live_db/` applies the real schema to a live ClickHouse 26.x
and Postgres 18 and runs representative queries, migrations, and the
projection DDL against them. It exists because the hermetic suite's mocks let
two real ClickHouse 26.x regressions ship green in the past; this tier catches
version- and dialect-level breakage. It is gated so it never runs by accident:

```bash
# ClickHouse on 9000, Postgres on 5433 (docker compose up -d clickhouse postgres)
cd backend
TMS_LIVE_DB_TESTS=1 uv run pytest tests/live_db/ -q
```

CI runs it in the Live-DB job against service containers pinned to the same
image versions as `docker-compose.yml`.

## Performance tier

`backend/tests/perf/` is the opt-in performance tier: scoring-engine
throughput, an ingestion parse-and-insert replay, and dashboard query latency
at a seeded warehouse volume, each judged against budgets in
`config/performance.yaml`. A Locust API and WebSocket load harness lives
alongside it. See [PERFORMANCE.md](PERFORMANCE.md) for methodology, how to run
each benchmark and the load harness, and how budgets are derived and ratified.

```bash
cd backend
uv sync --group perf
TMS_PERF_TESTS=1 uv run pytest tests/perf/ -q   # scoring runs with no services; the other two need ClickHouse
```

CI runs this tier in a separate `Performance` workflow (manual dispatch plus a
weekly schedule), which uploads the generated performance report as a build
artifact.

## Clustering sidecar

The optional clustering sidecar keeps its own suite under
`services/clustering/backend/tests/` (349 tests), covering its chain sources,
storage layer, scoring pipeline, and API. It runs in its own CI job.

```bash
cd services/clustering/backend
uv sync --extra dev
uv run pytest -q
```

## Frontend

The dashboard has a Vitest suite (`frontend/src/**/*.test.{ts,tsx}`, 40 tests)
over the API client and helper libraries, run under jsdom.

```bash
cd frontend
pnpm install
pnpm test        # vitest run
```

CI runs `pnpm lint`, `pnpm test`, and `pnpm build` in the Frontend job.

## Continuous integration

Every pull request, and every push to `main`, runs the `CI` workflow, with
these jobs:

- **Python lint (ruff + mypy)**: ruff format check, ruff lint, and mypy across both Python trees.
- **Backend (pytest + recall gate)**: the recall gate, then the full hermetic suite with coverage.
- **Live-DB tier (ClickHouse 26.x + Postgres)**: the 21 live-DB tests against real database containers.
- **Frontend (lint + build)**: lint, unit tests, and build.
- **Clustering sidecar (pytest)**: the sidecar suite.

CodeQL runs separately on pushes and pull requests. The `Performance` workflow
is dispatch/schedule-only (it needs its own database containers and is not on
the per-PR path).
