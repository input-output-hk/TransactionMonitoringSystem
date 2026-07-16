"""Collate performance artifacts into the customer-facing markdown report.

Reads every ``*.json`` artifact in the shared results directory (written by
the benchmark tier and the Locust harness through ``perf.results``; schema in
that module's docstring), plus the Locust CSV exports when present, and
renders one markdown document: a measured-versus-budget table per benchmark
family, the recorded environments, and the provenance of the budgets.

Runnable as a module, from ``backend/``:

    uv run python -m perf.report                        # print to stdout
    uv run python -m perf.report --output report.md     # write to a file

The results directory honours ``TMS_PERF_RESULTS_DIR`` (default
``<repo>/perf-results``), the same resolution the artifact writers use, so
the report always reads exactly what the benchmarks wrote. The generated
markdown follows the project documentation style rules: no em dashes, no
horizontal rules, colons in headings.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from perf import results

# Display-only precision for measured values: artifact writers already round
# to their own meaningful precision, so two decimals only trims float noise.
_DISPLAY_DECIMALS = 2

# git rev-parse --short=12 width, the abbreviation GitHub's UI resolves; full
# 64-char hashes would dominate the environment table without adding meaning.
_COMMIT_SHORT_HEX = 12

# Rendered timestamps: artifacts record ISO-8601 UTC; the report shows them
# at second precision, which is all a run-to-run comparison needs.
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S UTC"

# Verdict labels: failures are uppercase so they stand out when a reviewer
# scans the tables.
_VERDICT_PASS = "Pass"
_VERDICT_FAIL = "FAIL"

# Locust CSV export names: run_load.sh passes --csv <dir>/locust, and Locust
# appends the _stats suffix; the column names are Locust's own header row.
_LOCUST_STATS_FILENAME = "locust_stats.csv"
_CSV_TYPE = "Type"
_CSV_NAME = "Name"
_CSV_REQUESTS = "Request Count"
_CSV_FAILURES = "Failure Count"
_CSV_MEDIAN = "Median Response Time"
_CSV_P95 = "95%"
_CSV_RPS = "Requests/s"


@dataclass(frozen=True)
class _Family:
    """Presentation metadata for one known benchmark artifact. Verdicts are
    NOT re-derived here: each artifact carries its own budget checks (see
    perf.results), so the report renders judgments instead of encoding a
    second copy of the pass/fail rules that could drift from the benchmarks."""

    title: str
    description: str


# The four benchmark families, in report order. Unknown artifact names still
# render (with a generic title) so nothing recorded is dropped.
_FAMILIES: dict[str, _Family] = {
    "scoring_throughput": _Family(
        title="Scoring Throughput: Pure Compute Path",
        description=(
            "Times the exact per-transaction scoring call the analysis engine makes (all nine "
            "attack-class scorers, baselines stubbed in memory, no I/O) over a deterministic "
            "synthetic batch that mixes plain traffic with every attack-class shape."
        ),
    ),
    "ingestion_replay": _Family(
        title="Ingestion Replay: Parse and Insert Throughput",
        description=(
            "Replays a deterministic synthetic chain through the production ingestion path: "
            "Ogmios JSON parsing, then batched ClickHouse inserts through the same per-block "
            "call chain sync makes."
        ),
    ),
    "query_latency": _Family(
        title="Query Latency: Dashboard Queries at Seeded Volume",
        description=(
            "Samples the four representative dashboard queries against a warehouse seeded to "
            "preprod scale by perf.seed, judging each query's p95 against its latency ceiling."
        ),
    ),
    "api_load": _Family(
        title="API Load: Locust Read and WebSocket Scenario",
        description=(
            "Locust scenario against a running API: weighted dashboard readers over the REST "
            "read endpoints plus held-open WebSocket feed subscribers; judged on HTTP read p95 "
            "and sustained read request rate."
        ),
    ),
}


def _escape_cell(text: str) -> str:
    """Keep cell content from breaking the enclosing markdown table."""
    return text.replace("|", "\\|").replace("\n", " ")


def _fmt(value: Any) -> str:
    """Human-readable cell rendering for any artifact value."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if math.isfinite(value) and value == round(value):
            return f"{int(value):,}"
        return f"{value:,.{_DISPLAY_DECIMALS}f}"
    if isinstance(value, dict):
        return _escape_cell(", ".join(f"{k}={_fmt(v)}" for k, v in sorted(value.items())))
    if isinstance(value, list):
        return _escape_cell(", ".join(_fmt(v) for v in value))
    return _escape_cell(str(value))


def _fmt_csv(raw: str | None) -> str:
    """Render a Locust CSV field: numeric when parseable, verbatim otherwise
    (Locust writes N/A for percentiles of entries without response times)."""
    if raw is None or raw == "":
        return "n/a"
    try:
        return _fmt(float(raw))
    except ValueError:
        return _escape_cell(raw)


def _fmt_timestamp(iso: str | None) -> str:
    if not iso:
        return "n/a"
    try:
        recorded = datetime.fromisoformat(iso)
    except ValueError:
        return _escape_cell(iso)
    return recorded.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def _fmt_passed(passed: Any) -> str:
    if passed is None:
        return "not recorded"
    return _VERDICT_PASS if passed else _VERDICT_FAIL


def _load_artifacts(results_path: Path) -> list[dict[str, Any]]:
    """Load every JSON artifact; a truncated file (interrupted run) becomes an
    error entry in the report instead of killing report generation."""
    artifacts: list[dict[str, Any]] = []
    for path in sorted(results_path.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            artifacts.append({"name": path.stem, "error": str(exc)})
            continue
        if not isinstance(data, dict):
            artifacts.append({"name": path.stem, "error": "artifact is not a JSON object"})
            continue
        data.setdefault("name", path.stem)
        artifacts.append(data)
    return artifacts


def _ordered(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Known families in their canonical order, then anything else by name."""
    order = list(_FAMILIES)

    def key(artifact: dict[str, Any]) -> tuple[int, str]:
        name = artifact["name"]
        return (order.index(name) if name in order else len(order), name)

    return sorted(artifacts, key=key)


def _summary_section(artifacts: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Run Summary: One Verdict per Benchmark",
        "",
        "| Benchmark | Recorded at | Verdict |",
        "| --- | --- | --- |",
    ]
    for artifact in artifacts:
        if "error" in artifact:
            verdict = "unreadable artifact"
            recorded = "n/a"
        else:
            verdict = _fmt_passed(artifact.get("passed"))
            recorded = _fmt_timestamp(artifact.get("recorded_at"))
        lines.append(f"| {_escape_cell(artifact['name'])} | {recorded} | {verdict} |")
    lines.append("")
    return lines


def _environment_section(artifacts: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Environment: As Recorded per Run",
        "",
        "Benchmarks may run at different times and on different machines; every artifact "
        "records its own environment, reproduced here verbatim.",
        "",
        "| Benchmark | Python | Platform | Machine | Commit |",
        "| --- | --- | --- | --- | --- |",
    ]
    for artifact in artifacts:
        env = artifact.get("environment") or {}
        commit = env.get("git_commit")
        commit_cell = commit[:_COMMIT_SHORT_HEX] if isinstance(commit, str) else "n/a"
        lines.append(
            f"| {_escape_cell(artifact['name'])} | {_fmt(env.get('python'))} "
            f"| {_fmt(env.get('platform'))} | {_fmt(env.get('machine'))} | {commit_cell} |"
        )
    lines.append("")
    return lines


def _benchmark_section(artifact: dict[str, Any]) -> list[str]:
    name = artifact["name"]
    family = _FAMILIES.get(name)
    title = family.title if family else f"Benchmark: {name}"
    lines = [f"## {title}", ""]
    if "error" in artifact:
        lines += [f"Artifact `{name}.json` could not be read: {artifact['error']}", ""]
        return lines
    if family:
        lines += [family.description, ""]

    metrics: dict[str, Any] = artifact.get("metrics") or {}
    checks: list[dict[str, Any]] = artifact.get("checks") or []
    lines += ["| Metric | Measured | Budget | Verdict |", "| --- | --- | --- | --- |"]

    judged_metrics: set[str] = set()
    for chk in checks:
        metric = str(chk.get("metric"))
        judged_metrics.add(metric)
        budget_cell = f"{chk.get('comparison')} {_fmt(chk.get('budget'))}"
        lines.append(
            f"| {_escape_cell(metric)} | {_fmt(chk.get('measured'))} "
            f"| {budget_cell} | {_fmt_passed(chk.get('passed'))} |"
        )
    if not checks:
        lines.append("| (no budget judgments recorded in this artifact) |  |  |  |")

    # Context metrics carry no budget; they document the workload and the
    # distribution behind the judged numbers.
    for key in sorted(metrics):
        if key not in judged_metrics:
            lines.append(f"| {key} | {_fmt(metrics[key])} |  |  |")

    lines += ["", f"Verdict recorded by the run: {_fmt_passed(artifact.get('passed'))}.", ""]
    return lines


def _locust_section(results_path: Path) -> list[str]:
    path = results_path / _LOCUST_STATS_FILENAME
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    lines = [
        "## API Load: Locust Endpoint Breakdown",
        "",
        f"Per-endpoint request statistics from `{_LOCUST_STATS_FILENAME}` (exported by "
        "`backend/perf/run_load.sh`). Latencies are in milliseconds; the Aggregated row spans "
        "every entry. WebSocket arrival events carry no response time, so they never enter "
        "the latency percentiles.",
        "",
        "| Endpoint | Requests | Failures | Median (ms) | p95 (ms) | Requests/s |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        endpoint = " ".join(
            part for part in ((row.get(_CSV_TYPE) or ""), (row.get(_CSV_NAME) or "")) if part
        )
        cells = (
            _escape_cell(endpoint),
            _fmt_csv(row.get(_CSV_REQUESTS)),
            _fmt_csv(row.get(_CSV_FAILURES)),
            _fmt_csv(row.get(_CSV_MEDIAN)),
            _fmt_csv(row.get(_CSV_P95)),
            _fmt_csv(row.get(_CSV_RPS)),
        )
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _provenance_section() -> list[str]:
    return [
        "## Budget Provenance: config/performance.yaml",
        "",
        "Every budget above comes from `config/performance.yaml`, loaded through the validated "
        "`backend/perf/config.py` loader; the benchmarks and this report read the same file, so "
        "a budget cannot drift between the measurement and its write-up. The current values are "
        "provisional engineering guardrails derived from the first measured baseline (throughput "
        "floors at half the measured baseline, latency ceilings at twice the measured p95). "
        "Production targets are ratified with the customer against this report, then updated in "
        "that one file. Methodology: `docs/PERFORMANCE.md`.",
        "",
    ]


def build_report(results_path: Path) -> str:
    """Render the full markdown report from whatever the directory holds."""
    generated = datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)
    lines = [
        "# TMS Performance Report",
        "",
        f"Generated {generated} by `uv run python -m perf.report` from the artifacts in "
        f"`{results_path}`.",
        "",
        "## Purpose",
        "",
        "This report collates the latest recorded run of each performance benchmark family "
        "into one document: measured values judged against the budgets in "
        "`config/performance.yaml`. It is the evidence base for ratifying production "
        "performance targets with the customer. Regenerate it after any benchmark run; see "
        "`docs/PERFORMANCE.md` for how each number is produced.",
        "",
    ]
    artifacts = _ordered(_load_artifacts(results_path))
    if not artifacts:
        lines += [
            f"No benchmark artifacts found in `{results_path}`. Run the performance tier "
            "first, for example `TMS_PERF_TESTS=1 uv run pytest tests/perf/ -q` from "
            "`backend/` (see `docs/PERFORMANCE.md`).",
            "",
        ]
        return "\n".join(lines)

    lines += _summary_section(artifacts)
    lines += _environment_section(artifacts)
    for artifact in artifacts:
        lines += _benchmark_section(artifact)
    lines += _locust_section(results_path)
    lines += _provenance_section()
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m perf.report",
        description=(
            "Collate the perf-results artifacts (JSON via perf.results, plus Locust CSV "
            "exports) into the markdown performance report. Reads the directory named by "
            "TMS_PERF_RESULTS_DIR, defaulting to <repo>/perf-results."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the report to this file instead of stdout",
    )
    args = parser.parse_args(argv)
    report = build_report(results.results_dir())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")
        print(f"[perf.report] wrote {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
