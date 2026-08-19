# Repository Map

This project is delivered as a single repository containing three separately
specified deliverables: the backend, the frontend, and the alerting subsystem.
This document maps each of them onto the trees that hold it, so that a reader
holding the delivery specification can locate what it names.

Everything below was measured at the commit this document ships in, and every
figure is reproducible with the commands in
[Measuring this yourself](#measuring-this-yourself). If a number here disagrees
with what those commands report, trust the commands: the counts move with every
commit and this document is only as fresh as its last edit.

## Why one repository

The three deliverables are one deployable. `backend/Dockerfile` compiles the
frontend in its first stage and the FastAPI application serves the resulting
bundle, so the frontend has no runtime service of its own. The alerting
subsystem runs inside the backend process and reads the same configuration and
database schema. Splitting them would introduce a version-compatibility surface
between components that are always released together, and would fragment a
single CI gate that currently runs the recall suite before anything else is
allowed to merge.

The trade is that a deliverable boundary is a directory boundary rather than a
repository boundary. This document is what makes those boundaries explicit.

## Backend

The ingestion pipeline, the detection engine, the API, and the clustering
sidecar.

| Tree | Files | Lines | Contents |
|---|---|---|---|
| `backend/` | 240 | 54,567 | FastAPI application, ingestion, detection scorers, API, tests |
| `services/clustering/` | 169 | 30,123 | Clustering sidecar: its own deployable, own Python project, own CI job |
| `config/` | 2 | 1,029 | `detection.yaml` (913 lines), `performance.yaml` (116 lines) |

Counts exclude lockfiles (`services/clustering/backend/uv.lock`).

Within `backend/`: `backend/app/` is 100 Python files and 28,368 lines of source;
`backend/tests/` is 110 files and 21,500 lines. The largest application modules,
counted in Python files, are `analysis/` (26, the nine detection scorers), `api/`
(13), `notifications/` (12), `ingestion/` (10), and `db/` (8). The Alerting table
below counts every tracked file rather than only Python, so `notifications/`
appears there as 13: the thirteenth is `ADDING_A_CHANNEL.md`.

Within `services/clustering/`: `app/` is 68 Python files and 11,990 lines,
`tests/` is 42 files and 9,749 lines. Its ClickHouse schema is 12 SQL files
under `services/clustering/clickhouse/init/`.

`config/` belongs to the backend by ownership rather than by name:
`config/detection.yaml` is read only by `backend/app/analysis/`, and
`config/performance.yaml` only by `backend/perf/config.py`.

**Runtime:** Python 3.13 (`pyproject.toml` requires `>=3.13`, image
`python:3.13-slim`). Entrypoint `backend/run.py`, which runs `app.main:app`
under uvicorn on `API_PORT` (default 8000). The sidecar runs
`app.api.main:app` on its own port.

**Gated by:** the `Python lint (ruff + mypy)` job, which covers both Python
trees; `Backend (pytest + recall gate)`; `Live-DB tier (ClickHouse 26.x +
Postgres)`; `Clustering sidecar (pytest)`; and the weekly `Performance`
workflow.

**Documented in:** [ARCHITECTURE.md](ARCHITECTURE.md),
[C4-ARCHITECTURE.md](C4-ARCHITECTURE.md), [DATA-FLOW.md](DATA-FLOW.md),
[TMS_DETECTION_SPEC.md](TMS_DETECTION_SPEC.md), [CLUSTERING.md](CLUSTERING.md),
and the configuration reference in [RUNBOOK.md](../RUNBOOK.md).

## Frontend

The operator dashboard.

| Tree | Files | Lines | Contents |
|---|---|---|---|
| `frontend/` | 146 | 19,372 | React SPA: pages, components, API client, tests. Excludes `pnpm-lock.yaml` |

`frontend/src/` is 127 TypeScript and TSX files totalling 18,606 lines, of which
115 files and 16,485 lines are non-test source. It carries 12 page components
across 15 routes, 51 components, and 21 API-client modules.

**Stack, as built:** React 19.2.6 on TypeScript 6.0.2, bundled by Vite 8.1.2,
styled with Tailwind v4 through `@tailwindcss/vite`, routed by react-router-dom
7.18.1, server state through TanStack Query 5. Charts are Plotly; the entity
graph is Cytoscape with fcose layout; UI primitives are Radix. Tests run under
Vitest 4.1.9 in jsdom. Package manager pnpm (CI pins 11.8.0) on Node 22.

**Deployable:** none of its own. `backend/Dockerfile` builds it in stage 1 and
the application serves the bundle from `/app/frontend-dist`. The consequence
worth knowing is that `VITE_NETWORK` is a build argument baked into the bundle,
not a runtime setting: changing it requires `docker compose build app`, not a
restart.

**Gated by:** the `Frontend` CI job.

## Alerting

Detection-to-operator delivery: routing, channels, deduplication, the periodic
report, and the configuration surface.

Deduplication is two-level, which is worth knowing before reading the file table
because it accounts for two of the locations. `notified_alerts` answers "have we
already reported this transaction"; `notified_alert_groups` answers "have we
recently reported this script", and collapses a burst of findings that are one
situation into one alert per window. The second level bounds notification volume
only: every finding is still scored, stored and shown in the dashboard. Both
levels let an escalation to a higher band through immediately, so neither can
cost a detection. See
[ALERTING.md](ALERTING.md#per-group-one-alert-per-script-per-window).

This is the one deliverable that does not sit under a single prefix. It is 36
files and 6,092 lines across the seven locations below.

| Location | Files | Lines | Contents |
|---|---|---|---|
| `backend/app/notifications/` | 13 | 2,178 | Config schema and validator, dispatcher, trigger routing, payloads, report builder, channel registry, alert grouping, `channels/email.py`, `channels/webhook.py` |
| `backend/app/tasks/notifications.py` | 1 | 233 | Periodic-report scheduler and the `contract_anomaly` poller |
| `backend/app/api/notifications_config.py` | 1 | 108 | `GET`/`PUT /api/v1/notifications/config`, admin-gated |
| `backend/tests/notifications/` + `backend/tests/api/test_notifications_config.py` | 9 | 1,429 | 97 tests |
| `backend/scripts/webhook_testing/` | 6 | 555 | Three-tier delivery test harness and a reference receiver |
| `backend/tests/live_db/test_alert_grouping_pg.py` | 1 | 201 | Live-Postgres tests for the group-dedup ledger's band-escalation and window-expiry guards |
| `frontend/src` (5 files) | 5 | 1,388 | `NotificationsSettingsPage.tsx` (851), its test, the API client, and the pre-save config linter |

**Runtime configuration** is a single JSONB document in Postgres
(`notification_config`), edited through the admin UI or the config API and
hot-reloaded without a restart. The 16 `NOTIFY_*`, `WEBHOOK_*` and
`EMAIL_NOTIFY_*` environment settings are read at startup and need a restart;
they are documented in the [RUNBOOK configuration reference](../RUNBOOK.md).

**Gated by:** no CI job of its own. It is covered by the `Backend`, `Frontend`
and lint jobs.

**Documented in:** [ALERTING.md](ALERTING.md) for operators, and
`backend/app/notifications/ADDING_A_CHANNEL.md` for developers adding a
delivery channel.

## Shared

These belong to no single deliverable.

| Path | Serves |
|---|---|
| `docker-compose.yml` | All three: the full stack definition, eight services across four profiles |
| `clickhouse/config.d/log-retention.xml` | Infrastructure: bind-mounted into the ClickHouse container, bounds its own system logs |
| `.github/workflows/` | All three: five CI jobs plus the weekly performance workflow |
| `scripts/` | Operations: `start.sh`, `backup.sh`, `db.sh`, `reset.sh` |
| Root `README.md`, `RUNBOOK.md`, `docs/` | All three |

## Test inventory

1,938 automated tests across six tiers. Counts measured at this commit, not
quoted from an earlier report.

| Tier | Location | Tests |
|---|---|---|
| Backend hermetic | `backend/tests/` | 1,276 |
| Recall gate (subset of the above, run first and alone in CI) | `backend/tests/analysis/` | 554 |
| Backend live-DB | `backend/tests/live_db/` | 28 |
| Clustering sidecar | `services/clustering/backend/tests/` | 495 |
| Sidecar live-DB | `services/clustering/backend/tests/live_db/` | 5 |
| Frontend | `frontend/src/**/*.test.{ts,tsx}` | 131 |
| Performance | `backend/tests/perf/` | 3 |

See [TESTING.md](TESTING.md) for what each tier covers and how to run it.

## Measuring this yourself

```sh
# Tracked files and lines, per deliverable
git ls-files backend/ | wc -l
git ls-files backend/ | xargs wc -l | tail -1
git ls-files config/ | xargs wc -l | tail -1
git ls-files frontend/ | grep -v pnpm-lock | xargs wc -l | tail -1
git ls-files services/ | grep -v uv.lock | xargs wc -l | tail -1

# Subdivisions are filtered by language, so the per-tree figures above will not
# match them: `backend/app/` is quoted as Python files only, `frontend/src/` as
# TypeScript and TSX only.
git ls-files backend/app | grep '\.py$' | xargs wc -l | tail -1
git ls-files frontend/src | grep -E '\.tsx?$' | xargs wc -l | tail -1

# The alerting tree, which spans seven locations. All seven must be listed or
# the total falls short of the 36 files / 6,092 lines quoted above; the
# live-Postgres grouping test is the one easily missed.
git ls-files \
  backend/app/notifications backend/app/tasks/notifications.py \
  backend/app/api/notifications_config.py backend/tests/notifications \
  backend/tests/api/test_notifications_config.py backend/scripts/webhook_testing \
  backend/tests/live_db/test_alert_grouping_pg.py \
  frontend/src/pages/NotificationsSettingsPage.tsx \
  frontend/src/pages/NotificationsSettingsPage.test.tsx \
  frontend/src/lib/api/notifications.ts frontend/src/lib/notification-warnings.ts \
  frontend/src/lib/notification-warnings.test.ts | xargs wc -l | tail -1

# Test counts. Each runs in a subshell so the block is paste-safe from the
# repository root: a bare `cd` chain would leave the shell in backend/ and the
# next line would fail to find its directory.
#
# The two opt-in tiers are skipped at COLLECTION without their flag, so without
# it they report zero rather than their row in the table above. That is also why
# the hermetic count is unaffected by them.
(cd backend && ../.venv/bin/python -m pytest tests/ -q --co | tail -1)
(cd backend && TMS_LIVE_DB_TESTS=1 ../.venv/bin/python -m pytest tests/live_db -q --co | tail -1)
(cd backend && TMS_PERF_TESTS=1 ../.venv/bin/python -m pytest tests/perf -q --co | tail -1)
(cd services/clustering/backend && ./.venv/bin/python -m pytest -q --co | tail -1)
(cd frontend && pnpm vitest run)
```
