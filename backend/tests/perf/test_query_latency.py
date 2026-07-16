"""p95 latency benchmarks for the four representative dashboard queries.

Runs the real query code paths against a live ClickHouse seeded to preprod
scale by perf.seed (idempotent: reruns dedup instead of growing the tables):

    transactions_list  the GET /api/v1/transactions list read (handler called
                       directly; verify_api_key only runs via FastAPI DI)
    alert_timeseries   clickhouse_scores.get_alert_timeseries
    stats_summary      the GET /api/v1/transactions/stats/summary aggregate
    analysis_results   clickhouse_scores.get_class_scores_list (scored rows)

Each query is sampled query_latency.samples times with per-sample parameter
variation (different cursors, day windows, aggregate bounds, page offsets)
so ClickHouse cannot serve one cached result for every sample; p95 is judged
against query_latency.budgets_ms. Requires TMS_PERF_TESTS=1 (see conftest).
"""

import statistics
import time
from datetime import timedelta

import pytest

from app.api import transactions as transactions_api
from app.db import clickhouse_scores as scores
from perf import MS_PER_SECOND, PERF_NETWORK, p95, results
from perf import seed as perf_seed
from perf.config import load

# The list endpoints' documented default page size (app.api._params.PageLimit
# callers default to 100): the page shape dashboards actually request.
_LIST_PAGE_SIZE = 100

# Cursor shift between transactions_list samples: 30 minutes is ~150 rows at
# the seeded density (100k txs / 14 days), so every sample reads a distinct
# page window instead of one cacheable result.
_CURSOR_STEP = timedelta(minutes=30)

# stats_summary window bounds move by span/(this * samples) per sample: every
# sample aggregates a different [from, to) window, and even the tightest
# sample still covers at least half the seeded span.
_WINDOW_SHRINK_DIVISOR = 4

# verify_api_key runs only through FastAPI dependency injection; a direct
# handler call receives this placeholder verbatim and never validates it.
_DIRECT_CALL_API_KEY = "perf-tier-direct-call"


@pytest.fixture(scope="module")
def seeded(ch):
    """Preprod-scale dataset under PERF_NETWORK; seeds only when the live
    row counts are below the configured target (see perf.seed.ensure_seeded)."""
    return perf_seed.ensure_seeded(ch, load())


async def test_dashboard_query_p95_within_budget(seeded):
    qcfg = load().query_latency
    samples = qcfg.samples
    span_days = qcfg.seed.span_days
    anchor = perf_seed.dataset_anchor()
    window_start = anchor - timedelta(days=span_days)
    window_step = timedelta(days=span_days) / (samples * _WINDOW_SHRINK_DIVISOR)

    async def transactions_list(k: int) -> None:
        await transactions_api.get_transactions(
            network=PERF_NETWORK,
            limit=_LIST_PAGE_SIZE,
            before=anchor - k * _CURSOR_STEP,
            address=None,
            api_key=_DIRECT_CALL_API_KEY,
        )

    async def alert_timeseries(k: int) -> None:
        # The warmup call (k == samples) alone uses the full span, so no
        # timed sample reruns the warmup's exact query; timed samples cycle
        # day windows over [1, span_days - 1]. Windows do repeat once samples
        # exceeds the cycle length: dashboards re-issue identical timeseries
        # queries too, so cache-warm repeats are part of the latency under
        # measurement.
        timed_window_cycle = max(span_days - 1, 1)
        days = span_days if k == samples else 1 + (k % timed_window_cycle)
        await scores.get_alert_timeseries_async(PERF_NETWORK, days=days)

    async def stats_summary(k: int) -> None:
        await transactions_api.get_transaction_stats(
            network=PERF_NETWORK,
            time_from=window_start + k * window_step,
            time_to=anchor - k * window_step,
            api_key=_DIRECT_CALL_API_KEY,
        )

    async def analysis_results(k: int) -> None:
        await scores.get_class_scores_list_async(
            network=PERF_NETWORK,
            risk_band=None,
            attack_class=None,
            min_score=0.0,
            sort="score",
            limit=_LIST_PAGE_SIZE,
            offset=k * _LIST_PAGE_SIZE,
        )

    budgets = qcfg.budgets_ms
    surfaces = {
        "transactions_list": (transactions_list, budgets.transactions_list_p95),
        "alert_timeseries": (alert_timeseries, budgets.alert_timeseries_p95),
        "stats_summary": (stats_summary, budgets.stats_summary_p95),
        "analysis_results": (analysis_results, budgets.analysis_results_p95),
    }

    metrics: dict = {"samples_per_query": samples, "seeded_rows": dict(seeded)}
    checks: list[dict] = []
    for name, (runner, budget_ms) in surfaces.items():
        # One untimed warmup per query (k=samples keeps its parameters
        # distinct from every timed sample): executor-thread connection setup
        # and mark-cache population are one-time process costs, not per-query
        # dashboard latency.
        await runner(samples)
        timings_ms: list[float] = []
        for k in range(samples):
            started = time.perf_counter()
            await runner(k)
            timings_ms.append((time.perf_counter() - started) * MS_PER_SECOND)
        metrics[f"{name}_p50_ms"] = statistics.median(timings_ms)
        checks.append(results.check(f"{name}_p95_ms", p95(timings_ms), "<=", budget_ms))

    # Recorded BEFORE the asserts: a failed run must still leave an artifact
    # for the performance report, judged on the same checks.
    results.record("query_latency", metrics=metrics, checks=checks)

    # Measurement-validity guard: the p95s are only comparable against the
    # budgets if the warehouse actually held the configured volume.
    assert seeded["transactions"] >= qcfg.seed.transactions, (
        f"seeded volume {seeded['transactions']} below configured "
        f"{qcfg.seed.transactions}; latencies are not comparable to budgets"
    )
    failures = [
        f"{c['metric']}: p95 {c['measured']:.1f} ms > budget {c['budget']:.0f} ms"
        for c in checks
        if not c["passed"]
    ]
    assert not failures, "; ".join(failures)
