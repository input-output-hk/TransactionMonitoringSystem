"""Opt-in performance tier.

Benchmarks with budget assertions: scoring throughput, ingestion replay,
and warehouse query latency, judged against config/performance.yaml. They
are measurements, not correctness tests, so they stay out of the hermetic
suite: opt in with TMS_PERF_TESTS=1; without it the whole directory is
skipped at collection and `pytest tests/` behavior is unchanged.

Benchmarks that touch storage follow the live_db tier's conventions:
connection settings come from the normal app environment (with the repo
docker-compose defaults that means POSTGRES_PORT=5433 locally), and every
write is namespaced under perf.PERF_NETWORK so read paths, which are all
network-scoped, never surface synthetic rows to operator dashboards.

Shared plumbing (config loader, artifact recorder, PERF_NETWORK, workload
constants) lives in the perf package; tests import it directly rather than
through fixture indirection. Each benchmark records a JSON artifact via
perf.results; the report generator turns those into the customer-facing
performance report.
"""

import os

from tests.live_fixtures import (  # noqa: F401  (re-exported fixtures)
    ch,
    mock_clickhouse_baseline,
)

_PERF_ENV = "TMS_PERF_TESTS"

if not os.environ.get(_PERF_ENV):
    collect_ignore_glob = ["test_*.py"]
