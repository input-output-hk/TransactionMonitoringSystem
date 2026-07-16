#!/usr/bin/env bash
# Headless Locust run of the standard api_load scenario against a running TMS
# API, exporting CSV stats (and the api_load.json artifact) into the shared
# results directory for the report generator.
#
# The scenario shape lives in config/performance.yaml (api_load section) and
# is applied by the locustfile's init_command_line_parser hook, which is the
# single place defaults are decided: it also honours these overrides, so this
# script never restates them as flags.
#
#   TMS_PERF_HOST              target base URL          (default: api_load.host)
#   TMS_PERF_USERS             total users              (default: users + ws_subscribers)
#   TMS_PERF_SPAWN_RATE        users spawned per second (default: api_load.spawn_rate)
#   TMS_PERF_DURATION_SECONDS  run length in seconds    (default: api_load.duration_seconds)
#   TMS_PERF_RESULTS_DIR       CSV/JSON output dir      (default: <repo>/perf-results)
#   TMS_PERF_API_KEY           API key sent on every request (required unless
#                              the target runs in dev mode)
#
# Appended Locust flags win over both (argparse defaults only apply to
# options the command line omits):
#
#   TMS_PERF_API_KEY=... backend/perf/run_load.sh
#   TMS_PERF_API_KEY=... backend/perf/run_load.sh --host http://10.0.0.5:8000 -t 300s

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

# perf.config (and the locustfile's `from perf.config import load`) resolve
# from the backend tree, not the repo root, so it must lead PYTHONPATH.
export PYTHONPATH="$BACKEND_DIR${PYTHONPATH:+:$PYTHONPATH}"

cd "$REPO_ROOT"

RESULTS_DIR="${TMS_PERF_RESULTS_DIR:-$REPO_ROOT/perf-results}"
mkdir -p "$RESULTS_DIR"
# The locustfile's test_stop hook writes api_load.json through perf.results,
# which honours this variable; exporting it keeps the JSON artifact and the
# CSVs in the same directory.
export TMS_PERF_RESULTS_DIR="$RESULTS_DIR"

if [[ -z "${TMS_PERF_API_KEY:-}" ]]; then
  echo "run_load.sh: warning: TMS_PERF_API_KEY is not set;" \
    "only a dev-mode target (empty API_KEYS + TMS_ALLOW_DEV_MODE=1) will accept the run" >&2
fi

exec uv run --group perf locust \
  -f "$BACKEND_DIR/perf/locustfile.py" \
  --headless \
  --csv "$RESULTS_DIR/locust" \
  --only-summary \
  "$@"
