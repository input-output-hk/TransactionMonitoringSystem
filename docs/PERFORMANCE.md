# Performance: Methodology and Budgets

The performance tier answers one question: is the system still fast enough to do its job? For a
monitoring system that job is recall with low latency. Ingestion must keep pace with the chain,
scoring must keep pace with ingestion, and the operator dashboard must stay responsive at
retention-scale volumes; a throughput regression anywhere in that pipeline eventually turns into
a late alert. The tier measures each seam with a repeatable benchmark, judges the numbers against
budgets kept in one file (`config/performance.yaml`), and records every run as a JSON artifact
that a report generator collates into the customer-facing performance report.

The tier is opt-in and separate from the hermetic test suite: it measures, it does not test
correctness. Without `TMS_PERF_TESTS=1` the whole `backend/tests/perf/` directory is skipped at
collection time, so `pytest tests/` behaves exactly as before.

## The Four Benchmark Families: What Is Measured and Why

| Family | Measures | Needs | Code |
|---|---|---|---|
| Scoring throughput | Transactions scored per second, pure compute | Nothing | `backend/tests/perf/test_scoring_throughput.py` |
| Ingestion replay | Ogmios parse tx/s and warehouse insert rows/s | ClickHouse | `backend/tests/perf/test_ingestion_replay.py` |
| Query latency | p95 of four dashboard queries at seeded volume | ClickHouse | `backend/tests/perf/test_query_latency.py`, `backend/perf/seed.py` |
| API and WebSocket load | Read p95 and sustained request rate under concurrent users | A running API | `backend/perf/locustfile.py`, `backend/perf/run_load.sh` |

### Scoring Throughput: The Pure Compute Path

Times the exact per-transaction call the analysis engine's poll loop makes, with all nine
attack-class scorers enabled and baseline lookups stubbed in memory, so the timed region is pure
compute with no I/O. The workload is a deterministic synthetic batch (fixed seed) that mixes
plain wallet traffic, the dominant chain shape, with enough of every attack-class shape that all
nine scorers do real scoring work instead of short-circuiting on their gates; a warmup pass
verifies every class actually engages before anything is timed.

Why it matters: scoring lag is alert lag. If per-transaction compute regresses, the engine falls
behind ingestion during bursts and a real attack is flagged late. Budget:
`scoring.min_throughput_tps`, judged against the median of the timed batch repetitions.

```bash
cd backend
TMS_PERF_TESTS=1 uv run pytest tests/perf/test_scoring_throughput.py -q
```

No services are required; this benchmark runs anywhere the hermetic suite runs.

### Ingestion Replay: Parse and Insert Throughput

Replays a deterministic synthetic chain (`ingestion.blocks` blocks of `ingestion.txs_per_block`
transactions, five realistic payload shapes from simple payments to phase-2 script failures)
through the production ingestion path, producing two numbers: Ogmios v6 JSON parse throughput
(`min_parse_tps`), and batched ClickHouse insert throughput (`min_insert_rows_per_s`) through the
same per-block insert call chain sync makes, side-table writes included.

Why it matters: ingestion must outpace the chain with headroom for catch-up backfills; a
write-path regression stalls the entire detection pipeline behind it.

```bash
docker compose up -d clickhouse    # repo root
cd backend
TMS_PERF_TESTS=1 uv run pytest tests/perf/test_ingestion_replay.py -q
```

### Query Latency: Dashboards at Seeded Volume

Measures the p95 latency of the four representative dashboard queries (transactions list, alert
timeseries, stats summary, analysis results) through the real query code paths, against a
warehouse seeded to preprod scale: `query_latency.seed.transactions` transactions spread over
`span_days` days, a `scored_ratio` fraction carrying analysis score rows and an `alert_ratio`
fraction of those landing in alerting bands. Each query is sampled with varied parameters so
ClickHouse cannot serve one cached result. Budgets: `query_latency.budgets_ms`.

Why it matters: operators triage alerts through these views; a slow dashboard delays the response
to a live attack even when detection itself was on time.

The benchmark seeds itself when the namespace holds fewer rows than the configured target. The
seeder can also be run directly, and is idempotent (see the namespace section below):

```bash
cd backend
uv run python -m perf.seed                        # volumes from config/performance.yaml
uv run python -m perf.seed --transactions 10000   # explicit smaller volume
```

```bash
docker compose up -d clickhouse
cd backend
TMS_PERF_TESTS=1 uv run pytest tests/perf/test_query_latency.py -q
```

### API and WebSocket Load: The Serving Path End to End

A Locust scenario against a running API: `api_load.users` dashboard readers polling the REST read
endpoints at human pace plus `api_load.ws_subscribers` held-open WebSocket feed subscribers, for
`api_load.duration_seconds`. The other families isolate seams; this one measures the full serving
path (authentication, middleware, serialization, broadcast fan-out) under concurrent load.
Budgets: `api_load.budgets_ms.read_p95` and `api_load.min_rps`, computed over the HTTP reads only
so WebSocket events never dilute them.

```bash
uv sync --group perf                         # Locust is not in the default dev sync
docker compose up -d clickhouse postgres
cd backend && uv run python -m perf.seed     # realistic read volumes
# start the target so default-network reads serve the seeded namespace:
cd backend && CARDANO_NETWORK=perftest uv run uvicorn app.main:app --port 8000
export TMS_PERF_API_KEY=<one of the server's API_KEYS>
backend/perf/run_load.sh
```

See [backend/perf/README.md](../backend/perf/README.md) for the environment overrides, the
server-side rate-limit knobs a real load run needs raised, and how to read the exported CSVs.
This family does not run in CI (it needs a running app service); run it against a live
deployment.

## Running the Whole Tier

```bash
docker compose up -d clickhouse postgres     # repo root
cd backend
TMS_PERF_TESTS=1 uv run pytest tests/perf/ -q
```

A benchmark whose measurement misses its budget fails its test, so a regression is a red run,
not a number someone has to notice in a log.

## Synthetic Data: The perftest Namespace

Every row the tier writes is namespaced under network `perftest`. All application read paths are
network-scoped, so operator dashboards and API consumers never surface synthetic rows. Writes are
idempotent by construction: every row is a pure function of a fixed seed and its coordinates
(block, transaction, index), so a rerun regenerates byte-identical rows and the
ReplacingMergeTree tables collapse them instead of growing. Interrupting a seeding run is safe;
rerunning re-covers whatever a partial run wrote.

## Budgets: Where They Live and How They Are Derived

Every budget and workload knob lives in `config/performance.yaml`, loaded through the validated
loader in `backend/perf/config.py`; a typo in the file is a load-time error, not a silently
ignored knob, and benchmarks reference the config rather than duplicating values. The current
budgets are provisional, derived from the first measured baseline on a development machine using
two fixed rules:

- Throughput floors are set at 50% of the measured baseline.
- Latency ceilings are set at twice the measured p95.

The rules are chosen so normal machine variance (a laptop on battery, a busy CI runner) does not
flake the tier, while a real regression, halved throughput or doubled latency, still fails it.
These are engineering guardrails, not contractual SLOs: production targets are ratified with the
customer against the performance report, then updated in that one file. Numbers used for
ratification should come from the reference environment recorded in each artifact, not from a
shared CI runner.

## Artifacts and the Report: perf-results

Each benchmark run records one JSON artifact into `TMS_PERF_RESULTS_DIR` (default
`<repo>/perf-results`, gitignored): benchmark name, timestamp, environment (Python version,
platform, git commit), the measured metrics, the budgets the run was judged against, and the
verdict. Artifacts are written before the pass/fail assertion, so a failed run still leaves its
evidence. The Locust harness adds its CSV exports and an `api_load.json` verdict artifact to the
same directory.

The report generator collates everything in that directory into one markdown document, with a
measured-versus-budget table per family:

```bash
cd backend
uv run python -m perf.report                                      # print to stdout
uv run python -m perf.report --output ../docs/PERFORMANCE-REPORT.md
```

## CI: The Performance Workflow

`.github/workflows/perf.yml` (workflow name `Performance`) runs on manual dispatch and on a
weekly schedule (Mondays 03:43 UTC, off-peak). It provisions the same pinned ClickHouse and
Postgres service containers as the CI `live-db` job, seeds the warehouse, runs the benchmark tier
with budgets enforced, generates the report, and uploads `perf-results/` (JSON artifacts plus the
generated report) as a build artifact retained for 90 days: the approval trail budget
ratification points back to. A missed budget fails the workflow, and the report is generated and
uploaded regardless so the failing numbers are preserved. The Locust load harness is deliberately
excluded from CI because it needs a running app service to load.
