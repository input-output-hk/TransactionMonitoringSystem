# API and WebSocket Load Harness

Repeatable Locust load scenario against a running TMS API: weighted dashboard
readers over the REST read endpoints plus held-open WebSocket feed
subscribers. The scenario shape and its budgets live in one place,
`config/performance.yaml` (`api_load` section), loaded through the validated
`perf.config` loader; the harness exports Locust CSV stats and a JSON verdict
artifact into the shared results directory for the report generator.

## Prerequisites: stack and tooling

1. Bring up storage (from the repo root):

   ```bash
   docker compose up -d clickhouse postgres
   ```

2. Install the perf dependency group (Locust is not part of the default dev
   sync):

   ```bash
   uv sync --group perf
   ```

3. Start the API you want to load. Either the compose app service
   (`docker compose up -d app`, published on `127.0.0.1:8000`) or a local
   uvicorn:

   ```bash
   cd backend && CARDANO_NETWORK=perftest uv run uvicorn app.main:app --port 8000
   ```

## Seeding: the perftest namespace

Seed deterministic workload data first, so reads measure realistic row
volumes instead of empty tables:

```bash
cd backend && uv run python -m perf.seed
```

All seeded rows are namespaced under network `perftest`. The API's `?network=`
query parameter only accepts the public networks (`mainnet`, `preprod`,
`preview`), so the harness deliberately omits it and the server's default
network decides what the reads hit. To measure against the seeded namespace,
start the target API with `CARDANO_NETWORK=perftest` as shown above. Pointing
the harness at a normally configured instance also works; you are then
measuring whatever that instance's network contains.

## Authentication: TMS_PERF_API_KEY

The harness reads the API key from the environment and sends it on every
REST request (header `X-API-Key`, or whatever the server's `API_KEY_HEADER`
is renamed to, mirrored via `TMS_PERF_API_KEY_HEADER`) and on the WebSocket
handshake (as the `?api_key=` query parameter, the only channel browsers
support on upgrades):

```bash
export TMS_PERF_API_KEY=<one of the server's API_KEYS>
```

No key is ever hardcoded. Without it, only a dev-mode target (empty
`API_KEYS` plus `TMS_ALLOW_DEV_MODE=1`) accepts the run; a keyed deployment
answers 401 on REST and close code 4403 on `/ws`.

## Running: run_load.sh

```bash
backend/perf/run_load.sh
```

Defaults come from `config/performance.yaml`: `users` dashboard readers plus
`ws_subscribers` feed subscribers (Locust's `-u` total is their sum), spawned
at `spawn_rate`/s, for `duration_seconds`, against `host`. Override with
environment variables or by appending Locust flags, which win because they
are passed last:

```bash
TMS_PERF_DURATION_SECONDS=300 backend/perf/run_load.sh
backend/perf/run_load.sh --host http://10.0.0.5:8000 -u 100 -r 10
```

Env overrides: `TMS_PERF_HOST`, `TMS_PERF_USERS`, `TMS_PERF_SPAWN_RATE`,
`TMS_PERF_DURATION_SECONDS`, `TMS_PERF_RESULTS_DIR`. Note when passing `-u`
by hand: the WebSocket subscriber count is a fixed carve-out
(`ws_subscribers` via Locust's `fixed_count`), so keep the total at or above
`ws_subscribers` plus the readers you want.

Running Locust directly also works, because the locustfile installs the
config values as argparse defaults:

```bash
PYTHONPATH=backend uv run --group perf locust -f backend/perf/locustfile.py --headless
```

## Results: where the numbers land

Everything goes to `TMS_PERF_RESULTS_DIR` (default `<repo>/perf-results`,
gitignored, shared with the pytest perf tier):

| File | Content |
| --- | --- |
| `locust_stats.csv` | Per-endpoint and aggregated request stats (count, failures, percentiles, RPS) |
| `locust_stats_history.csv` | Time series of the same stats over the run |
| `locust_failures.csv` | Failure breakdown per endpoint |
| `locust_exceptions.csv` | Uncaught task exceptions, normally empty |
| `api_load.json` | perf.results artifact: metrics, the budgets applied, and pass/fail |

## Budgets: how config values map to the run

- `api_load.budgets_ms.read_p95` is the ceiling for the 95th percentile
  latency of the REST read endpoints: the `95%` column of the Aggregated row
  in the Locust summary and `locust_stats.csv`. The `api_load.json` artifact
  computes it over the HTTP entries only, so WebSocket pseudo-requests never
  dilute it (their arrivals are counted with no response time).
- `api_load.min_rps` is the floor for sustained aggregate request rate: the
  `Requests/s` of the same Aggregated row, again computed HTTP-only in the
  artifact.
- The artifact's `passed` field is true when both hold and at least one read
  completed.

WebSocket health is reported alongside, without a budget yet: connect count
and p95 connect latency (`/ws [connect]`), broadcast events received across
all subscribers (`/ws [broadcast]`), keepalive pongs (`/ws [pong]`), and
session-level failures (`/ws [session]`, carrying the server close code and
the knob to fix).

## Server knobs for load runs

Production-posture defaults in `backend/app/config.py` throttle exactly the
traffic this harness generates, all of it arriving from one IP and one API
key. For a load run, raise them on the TARGET (env vars for the app service);
do not fall back to dev mode:

- `RATE_LIMIT_REQUESTS` (default 240 per `RATE_LIMIT_WINDOW_SECONDS`, 60 s):
  the standard 25-reader scenario sustains far more than 4 req/s on one key,
  so leave `RATE_LIMIT_ENABLED=true` but raise the budget, for example
  `RATE_LIMIT_REQUESTS=100000`. A run drowning in 429s means this knob.
- `WS_HANDSHAKE_RATE_LIMIT_REQUESTS` (default 30 per
  `WS_HANDSHAKE_RATE_LIMIT_WINDOW_SECONDS`, 60 s, per client IP): 50
  subscribers from one IP exceed it in a burst. The harness therefore
  staggers connects at `TMS_PERF_WS_CONNECT_INTERVAL` seconds (default 2.5,
  derived from these server defaults with margin), which keeps a
  default-configured server accepting but takes about 2 minutes to reach
  full fan-out. For the standard 120 s scenario, raise the server knob (for
  example `WS_HANDSHAKE_RATE_LIMIT_REQUESTS=300`) and set
  `TMS_PERF_WS_CONNECT_INTERVAL=0.2` so all subscribers connect in the first
  seconds. Rejections surface as `/ws [session]` failures with close code
  4429.
- `WS_MAX_CONNECTIONS` (default 100): must stay at or above
  `ws_subscribers`; the same 4429 close fires when the cap is hit.
- `CORS_ALLOW_ORIGINS`: the harness sends no Origin header (always allowed);
  if you want to exercise the origin check, set `TMS_PERF_WS_ORIGIN` to a
  listed origin. An unlisted one is rejected with close code 1008.
