# Testing

This document is the test-suite inventory for the Cardano Transaction
Monitoring System: what each tier covers, how to run it, and how the tiers
map to CI. Counts were measured on the commit this document ships in and move as
the suite grows; the exact numbers are always whatever CI reports on the
latest commit.

## Test tiers at a glance

| Tier | Location | Count | Services needed | CI job |
|---|---|---|---|---|
| Backend hermetic | `backend/tests/` | 1478 | none (all I/O mocked) | Backend (pytest + recall gate) |
| Recall gate | `backend/tests/analysis/` | 666 (subset of the above) | none | Backend (run first, on its own) |
| Live-DB tier | `backend/tests/live_db/` | 60 | ClickHouse + Postgres | Live-DB tier (ClickHouse 26.x + Postgres) |
| Sidecar live-DB tier | `services/clustering/backend/tests/live_db/` | 8 | ClickHouse | Live-DB tier (ClickHouse 26.x + Postgres) |
| Performance tier | `backend/tests/perf/` | 3 | ClickHouse (2 of 3) | Performance (separate workflow) |
| Clustering sidecar | `services/clustering/backend/tests/` | 497 | none | Clustering sidecar (pytest) |
| Frontend | `frontend/src/**/*.test.{ts,tsx}` | 168 | none | Frontend (lint + build) |
| End-to-end | `frontend/e2e/` | 11 | the whole stack (app + Postgres + ClickHouse + Mailpit), via `./scripts/e2e.sh` | E2E (full stack) |

That is 2,225 tests across the seven independent tiers (the recall gate is a
subset of the backend suite, not an additional tier, so it is not added into
the total). A CI step re-collects every tier on each run and fails the build if
this table drifts.

The default developer command, `pytest tests/` from `backend/`, runs the
backend hermetic tier in the table above and nothing that needs a database: the
live-DB and performance tiers are opt-in behind environment flags so a
contributor without Docker still gets a green run.

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

`backend/tests/analysis/` is the full scorer and analysis suite. It contains
the attack-must-fire tests that prove each detection scorer still fires on its
real-attack case, alongside the must-not-fire precision cases and the engine,
feature and baseline tests they depend on. CI runs it first
and on its own so a recall regression is unambiguous, and the project's change
rules require every detection-parameter change to keep it green.

The must-fire cases are marked rather than merely named, so the guarantee is a
set you can run instead of a convention you have to trust:

```bash
cd backend
uv run pytest tests/analysis/scorers/ -m attack_must_fire -q
```

Each marked case asserts that a real attack scores at or above a band constant.
`backend/scripts/check_recall_markers.py` runs in CI and fails the build if any
scorer class stops having one, or if a case is marked without pinning a band.

It is a CI job, so its result has to be read; configuring it as a required
status check is a repository setting rather than something this suite can
assert.

### Coverage

CI measures line coverage on the full backend suite (`--cov=app`) and reports
it in the job summary; there is no enforced threshold yet (report-only). At the
commit this document ships in, backend line coverage is 76% over the `app`
package (`cd backend && ../.venv/bin/python -m pytest tests/ --cov=app`), and the
clustering sidecar is 85%. CI has reported a point lower than a local run on the
same commit; the difference is environment-gated branches.

## Live-DB integration tier

`backend/tests/live_db/` applies the real schema to a live ClickHouse 26.x
and Postgres 18 and runs representative queries, migrations, the projection
DDL, and the alert-grouping ledger's suppression semantics against them. It exists because the hermetic suite's mocks let
two real ClickHouse 26.x regressions ship green in the past; this tier catches
version- and dialect-level breakage. It is gated so it never runs by accident:

```bash
# ClickHouse on 9000, Postgres on 5433 (docker compose up -d clickhouse postgres)
cd backend
TMS_LIVE_DB_TESTS=1 uv run pytest tests/live_db/ -q
```

The clustering sidecar has its own tier under
`services/clustering/backend/tests/live_db/` (same gate), which executes the
query text introduced with the pre-deployment history backfill against a live
ClickHouse: the hybrid host-UNION-local reads, the immutability-boundary
aggregates, the host-membership publish bound, and the source-tagged cursor
round trip. It needs both schemas applied; `app.cli migrate` creates the
module's, and connection settings come from the `CLICKHOUSE_*` environment
(with the repo docker-compose defaults that means `CLICKHOUSE_USER=default`,
empty password, HTTP port 8123).

```bash
cd services/clustering/backend
uv run python -m app.cli migrate --init-dir ../clickhouse/init
TMS_LIVE_DB_TESTS=1 uv run pytest tests/live_db/ -q
```

CI runs both tiers in the Live-DB job against service containers pinned to the
same image versions as `docker-compose.yml`; the sidecar step runs after the
host step, which creates the `tms_analytics` tables its host arms read.

## Performance tier

`backend/tests/perf/` is the opt-in performance tier: scoring-engine
throughput, an ingestion parse-and-insert replay, and dashboard query latency
at a seeded warehouse volume, each judged against budgets in
`config/performance.yaml`. A Locust API and WebSocket load harness lives
alongside it. See [PERFORMANCE.md](PERFORMANCE.md) for methodology, how to run
each benchmark and the load harness, and how budgets are derived and tightened.

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
`services/clustering/backend/tests/` (counted in the table above), covering its chain sources,
storage layer, scoring pipeline, and API. It runs in its own CI job. Its
opt-in live tier (`tests/live_db/`, gated like the host's) is described in
the Live-DB section above.

```bash
cd services/clustering/backend
uv sync --extra dev
uv run pytest -q
```

## Frontend

The dashboard has a Vitest suite (`frontend/src/**/*.test.{ts,tsx}`, counted in
the table above) under jsdom, over the API client, the helper libraries, and rendered
components and pages (the datum tree, the transaction-detail panels, the alerts
page and the attack-detail page).

```bash
cd frontend
pnpm install
pnpm test        # vitest run
```

CI runs `pnpm lint`, the Vitest suite, and `pnpm build` in the Frontend job. The
test step writes JUnit XML, which the next step reads to check the count in the
table above, so the suite runs once rather than twice.

## End-to-end tier

Every tier above stubs something: the hermetic suites mock their I/O, the
live-DB tier drives the API through `TestClient` rather than a served
application, and the Vitest suite renders components without a backend. So all
of them can pass while the assembled stack does not come up, or comes up and
cannot complete a workflow an operator actually performs. This tier closes that
gap: it builds the production image from source, starts it with Postgres,
ClickHouse and Mailpit, and drives the served dashboard in a real browser
(Playwright, `frontend/e2e/`).

```bash
./scripts/e2e.sh                    # up, seed, test, tear down
E2E_KEEP_STACK=1 ./scripts/e2e.sh   # leave the stack up to inspect it
./scripts/e2e.sh --headed           # arguments pass through to Playwright
```

One script owns the whole tier, so a local run and the `E2E (full stack)` CI job
are the same run. It builds the image, waits for `/health`, seeds deterministic
findings (`backend/scripts/e2e/seed.py`: eight scored transactions, six of them
in the dashboard's default High + Critical view), bootstraps an admin through
`app.cli create-admin` and hands the magic link to Playwright, then tears the
stack down. Isolation is deliberate: its own compose project, container names,
volumes and host ports, with `.env.e2e` as the app's only environment source, so
the tier neither collides with a running dev stack nor inherits a developer's
`.env`.

What the specs cover, chosen as the workflows an operator performs and the
failure paths that matter:

| Spec | Covers |
|---|---|
| `stack-readiness` | `/health`, the unauthenticated redirect to sign-in, and the dashboard shell rendering with no console errors |
| `alert-review` | seeded detections in the default severity view, the filter widening the result set, and an alert's full risk score with its sub-scores |
| `archive-restore` | archiving as a false positive (the reason is required), the audit trail in the Archive, and the restore round-trip |
| `csv-import-export` | importing externally sourced attack data with per-row validation and an explicit confirm, then exporting the archive |
| `reports-export` | the reporting window and its CSV export |
| `roles` | inviting a Reviewer, redeeming the invite from Mailpit in a fresh browser, and the admin-only area denied both in the UI and with **403** at the API |

The seed is a pure function of its row index and the score table dedups on
`(network, tx_hash)`, so re-running the tier does not accumulate data. The
specs leave the data as they found it (the archive spec restores what it
archived), and the one row they genuinely create, the invited Reviewer, uses a
per-run address.

## Continuous integration

Every pull request, and every push to `main`, runs the `CI` workflow, with
these jobs:

- **Python lint (ruff + mypy)**: ruff format check, ruff lint, and mypy across both Python trees, plus the two text-only checks (published totals, traceability matrix).
- **Backend (pytest + recall gate)**: the recall-marker check, the recall gate, then the full hermetic suite with coverage.
- **Live-DB tier (ClickHouse 26.x + Postgres)**: the host live-DB tests, then the sidecar's live tier (schema via `app.cli migrate`), against real database containers.
- **Frontend (lint + build)**: lint, unit tests, and build.
- **Clustering sidecar (pytest)**: the sidecar suite.
- **E2E (full stack)**: `./scripts/e2e.sh`, which builds the image, brings the stack up, seeds, and drives the dashboard in a browser.

CodeQL runs separately on pushes and pull requests. The `Performance` workflow
is dispatch/schedule-only (it needs its own database containers and is not on
the per-PR path).
