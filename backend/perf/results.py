"""Result artifacts for the performance tier.

Every benchmark records one JSON file per run into the results directory
(``TMS_PERF_RESULTS_DIR``, default ``<repo root>/perf-results``, gitignored).
The report generator collates these files, plus the Locust CSV exports,
into the customer-facing performance report; keeping the schema in one
module means the writer and the reader cannot drift apart.

Judgments travel WITH the artifact: each benchmark builds its budget
comparisons via :func:`check` and records them, so the report renders
verdicts instead of re-deriving them, and a new metric or budget can never
drift between the benchmark's pass/fail logic and the report's tables.

Artifact schema (one JSON object per file):
    name         benchmark identifier, also the filename stem
    recorded_at  ISO-8601 UTC timestamp
    environment  python version, platform, git commit (best effort)
    metrics      measured values, benchmark-specific keys
    checks       list of budget judgments from check(): metric, measured,
                 comparison ('>=' floor or '<=' ceiling), budget, passed
    passed       True iff every check passed
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from perf import find_repo_root

_RESULTS_ENV = "TMS_PERF_RESULTS_DIR"
_RESULTS_DIRNAME = "perf-results"

# Single rounding rule for every float in an artifact: sub-microsecond noise
# in millisecond/second metrics is measurement jitter, not signal, and one
# rule keeps precision consistent across benchmark families.
_ARTIFACT_FLOAT_DECIMALS = 3

# The two budget directions: a floor the measurement must stay at or above
# (throughput), or a ceiling it must stay at or below (latency).
_FLOOR = ">="
_CEILING = "<="


def results_dir() -> Path:
    override = os.environ.get(_RESULTS_ENV)
    base = Path(override) if override else find_repo_root() / _RESULTS_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=find_repo_root(),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def environment() -> dict[str, Any]:
    """Capture what the report needs to reproduce or contextualize a run."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "git_commit": _git_commit(),
    }


def _rounded(value: Any) -> Any:
    """Apply the artifact-wide float precision recursively."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, _ARTIFACT_FLOAT_DECIMALS)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    return value


def check(metric: str, measured: float, comparison: str, budget: float) -> dict[str, Any]:
    """One budget judgment: '>=' judges a floor, '<=' a ceiling."""
    if comparison == _FLOOR:
        passed = measured >= budget
    elif comparison == _CEILING:
        passed = measured <= budget
    else:
        raise ValueError(f"comparison must be '{_FLOOR}' or '{_CEILING}', got {comparison!r}")
    return {
        "metric": metric,
        "measured": measured,
        "comparison": comparison,
        "budget": budget,
        "passed": passed,
    }


def record(
    name: str,
    *,
    metrics: dict[str, Any],
    checks: list[dict[str, Any]],
) -> Path:
    """Write one result artifact and return its path.

    ``passed`` is derived from the checks so a benchmark cannot record a
    verdict that disagrees with its own judgments.
    """
    path = results_dir() / f"{name}.json"
    payload = {
        "name": name,
        "recorded_at": datetime.now(UTC).isoformat(),
        "environment": environment(),
        "metrics": _rounded(metrics),
        "checks": _rounded(checks),
        "passed": all(c["passed"] for c in checks),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    return path
