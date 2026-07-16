"""Performance harness package.

Holds everything the opt-in performance tier shares that is not itself a
test: the validated budget/workload config loader (``perf.config``), the
warehouse seeder, the Locust load scenarios, and the report generator.
Benchmarks that assert against budgets live in ``backend/tests/perf`` and
import from here; nothing under this package is imported by ``app``.
"""

import statistics
from pathlib import Path

# Namespace every synthetic row is written under, single source of truth for
# the seeder, the benchmarks, and the tier conftest. Every app read path is
# network-scoped, so rows in this namespace never surface on operator
# dashboards regardless of volume.
PERF_NETWORK = "perftest"

# Fixed seed for every benchmark that fabricates synthetic data. Shared so a
# rerun regenerates byte-identical workloads (idempotent ReplacingMergeTree
# upserts, throughput changes attributable to code rather than a reshuffled
# workload) and so the benchmark families draw comparable distributions. The
# value is arbitrary but must never vary per run.
WORKLOAD_SEED = 901

# A Cardano policy id is a blake2b-224 script hash: 28 bytes = 56 hex
# characters (Cardano ledger spec, Mary-era multi-asset).
POLICY_ID_HEX_CHARS = 56

# SI conversion shared by every latency metric: budgets are phrased in
# milliseconds while time.perf_counter measures seconds.
MS_PER_SECOND = 1000.0

# statistics.quantiles(n=100) returns the 99 cut points P1..P99; index 94 is
# P95. Private to p95() so every family computes the tail the same way.
_PERCENTILE_GRID = 100
_P95_CUT_INDEX = 94


def p95(values: list[float]) -> float:
    """The tier's canonical tail statistic (95th percentile)."""
    # A single sample is its own tail; statistics.quantiles requires at
    # least two data points, and a samples=1 config is valid for quick runs.
    if len(values) < 2:
        return values[0]
    return statistics.quantiles(values, n=_PERCENTILE_GRID)[_P95_CUT_INDEX]


def find_repo_root() -> Path:
    """Walk up to the checkout root, identified by config/performance.yaml.

    Single locator for the config loader and the results writer, so the two
    can never disagree about where the repo (and its default paths) lives.
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "config" / "performance.yaml").is_file():
            return ancestor
    raise FileNotFoundError(
        f"Could not locate the repo root (config/performance.yaml) above {here}."
    )
