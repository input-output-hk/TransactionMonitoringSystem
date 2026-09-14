#!/bin/bash
# Bring up the full stack from source, seed deterministic findings, and run the
# Playwright E2E tier against it.
#
# Usage:
#   ./scripts/e2e.sh            # up, seed, test, tear down
#   E2E_KEEP_STACK=1 ./scripts/e2e.sh   # leave the stack running afterwards
#
# The tier is fully isolated from a developer's stack: its own compose project
# (tms-e2e), unique container names (docker-compose.e2e.yml), its own volumes,
# offset host ports, and .env.e2e as the ONLY environment source, so nothing
# from a local .env leaks in. CI's "E2E (full stack)" job runs this same
# script, so a local run reproduces the job exactly.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROJECT_NAME="tms-e2e"
# Absolute paths: the Playwright step runs from frontend/, and the EXIT trap
# tears the stack down from whatever directory the script died in. Relative
# -f paths would resolve against that cwd and the teardown would silently skip,
# leaking the stack (and its data) into the next run.
COMPOSE=(docker compose -f "$ROOT/docker-compose.yml" -f "$ROOT/docker-compose.e2e.yml" --env-file "$ROOT/.env.e2e")

# The ports .env.e2e publishes on; kept in one place for the URLs below.
API_PORT="$(grep '^API_PORT=' "$ROOT/.env.e2e" | cut -d= -f2)"
MAILPIT_HTTP_PORT="$(grep '^MAILPIT_HTTP_PORT=' "$ROOT/.env.e2e" | cut -d= -f2)"
export E2E_BASE_URL="http://127.0.0.1:${API_PORT}"
export E2E_MAILPIT_URL="http://127.0.0.1:${MAILPIT_HTTP_PORT}"

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "── app logs (tail) ──"
        "${COMPOSE[@]}" logs --no-color app 2>/dev/null | tail -60 || true
    fi
    if [ "${E2E_KEEP_STACK:-0}" != "1" ]; then
        # -v: the tier owns its volumes, and a surviving database would carry
        # the previous run's users and archive rows into the next one.
        "${COMPOSE[@]}" --profile app --profile mail down -v --remove-orphans > /dev/null 2>&1 || true
    else
        echo "E2E_KEEP_STACK=1: stack left running at ${E2E_BASE_URL}"
    fi
    exit "$status"
}
trap cleanup EXIT

echo "[1/5] build + start the stack (app, postgres, clickhouse, mailpit)"
"${COMPOSE[@]}" --profile app --profile mail up -d --build

echo "[2/5] wait for /health"
for _ in $(seq 1 60); do
    if curl -sf "${E2E_BASE_URL}/health" > /dev/null 2>&1; then break; fi
    sleep 2
done
curl -sf "${E2E_BASE_URL}/health" > /dev/null || {
    echo "ERROR: app never became healthy at ${E2E_BASE_URL}" >&2
    exit 1
}

echo "[3/5] seed deterministic findings"
"${COMPOSE[@]}" exec -T app python -m scripts.e2e.seed

echo "[4/5] bootstrap the admin and capture the magic link"
CLI_OUT="$("${COMPOSE[@]}" exec -T app python -m app.cli create-admin e2e-admin@example.com "E2E Admin" --no-email)"
TOKEN="$(printf '%s' "$CLI_OUT" | grep -o 'verify?token=[A-Za-z0-9._~-]*' | head -1 | cut -d= -f2)"
if [ -z "$TOKEN" ]; then
    echo "ERROR: no magic-link token in create-admin output:" >&2
    printf '%s\n' "$CLI_OUT" >&2
    exit 1
fi
# Rebuilt against E2E_BASE_URL rather than trusting APP_BASE_URL, so a port
# override in .env.e2e cannot strand the login on the wrong origin.
export E2E_ADMIN_MAGIC_LINK="${E2E_BASE_URL}/auth/verify?token=${TOKEN}"

echo "[5/5] run the Playwright suite"
cd "$ROOT/frontend"
pnpm exec playwright test "$@"
