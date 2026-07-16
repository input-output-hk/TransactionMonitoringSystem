"""Validated loader for ``config/performance.yaml``.

Mirrors the ``config/detection.yaml`` loading contract from
``app.analysis.scorer_config``: the file is located by walking up from this
module (override with ``TMS_PERF_CONFIG``), parsed once, and validated
eagerly so a bad edit fails the tier at collection time with a message that
names the offending key, not deep inside a benchmark.

Budgets are grouped per benchmark family; each pydantic model matches one
top-level section of the YAML. ``extra="forbid"`` makes a typo in the YAML a
load-time error instead of a silently ignored knob.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from perf import find_repo_root

_CONFIG_ENV = "TMS_PERF_CONFIG"
_CONFIG_RELPATH = Path("config") / "performance.yaml"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScoringBudget(_StrictModel):
    batch_size: int = Field(gt=0)
    iterations: int = Field(gt=0)
    min_throughput_tps: float = Field(gt=0)


class IngestionBudget(_StrictModel):
    blocks: int = Field(gt=0)
    txs_per_block: int = Field(gt=0)
    min_parse_tps: float = Field(gt=0)
    min_insert_rows_per_s: float = Field(gt=0)


class QuerySeed(_StrictModel):
    transactions: int = Field(gt=0)
    span_days: int = Field(gt=0)
    scored_ratio: float = Field(ge=0.0, le=1.0)
    alert_ratio: float = Field(ge=0.0, le=1.0)


class QueryLatencyBudgets(_StrictModel):
    transactions_list_p95: float = Field(gt=0)
    alert_timeseries_p95: float = Field(gt=0)
    stats_summary_p95: float = Field(gt=0)
    analysis_results_p95: float = Field(gt=0)


class QueryLatencyBudget(_StrictModel):
    seed: QuerySeed
    samples: int = Field(gt=0)
    budgets_ms: QueryLatencyBudgets


class ApiLoadBudgets(_StrictModel):
    read_p95: float = Field(gt=0)


class ApiLoadBudget(_StrictModel):
    host: str
    users: int = Field(gt=0)
    spawn_rate: int = Field(gt=0)
    duration_seconds: int = Field(gt=0)
    ws_subscribers: int = Field(ge=0)
    think_time_min_seconds: float = Field(gt=0)
    think_time_max_seconds: float = Field(gt=0)
    page_sizes: list[int] = Field(min_length=1)
    budgets_ms: ApiLoadBudgets
    min_rps: float = Field(gt=0)
    max_read_failure_ratio: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _think_time_window_ordered(self) -> ApiLoadBudget:
        if self.think_time_max_seconds < self.think_time_min_seconds:
            raise ValueError(
                "think_time_max_seconds must be >= think_time_min_seconds; "
                "the pair defines the readers' idle window"
            )
        return self


class PerformanceConfig(_StrictModel):
    scoring: ScoringBudget
    ingestion: IngestionBudget
    query_latency: QueryLatencyBudget
    api_load: ApiLoadBudget


def _config_path() -> Path:
    override = os.environ.get(_CONFIG_ENV)
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(
                f"{_CONFIG_ENV} points at {path}, which does not exist or is not a file."
            )
        return path
    # Honour the app's relocated-config-directory mechanism (the same one
    # scorer_config uses for detection.yaml), but fall back to the checkout
    # when the relocated directory carries no budgets: deployments that move
    # runtime config do not have to ship the perf tier's dev/CI artifact.
    config_dir = os.environ.get("TMS_CONFIG_DIR")
    if config_dir:
        candidate = Path(config_dir) / _CONFIG_RELPATH.name
        if candidate.is_file():
            return candidate
    return find_repo_root() / _CONFIG_RELPATH


@lru_cache(maxsize=1)
def load() -> PerformanceConfig:
    """Load and validate the performance config; cached for the process."""
    path = _config_path()
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    try:
        return PerformanceConfig(**data)
    except ValidationError as exc:
        raise ValueError(
            f"Invalid performance config at {path}: {exc}. "
            "Fix the value in config/performance.yaml; every budget and "
            "workload knob for the perf tier lives there."
        ) from exc
