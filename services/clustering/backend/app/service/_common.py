"""Shared helpers + tunables for the service submodules: the job-progress sink,
the client-safe error mapper, DBSCAN parameter selection, and the feature loader."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.clustering.evaluate import FALLBACK_EPS, MIN_POINTS, MIN_SAMPLES_FLOOR
from app.config import get_settings
from app.features import build_features
from app.ingest.ingester import IngestResult, ProgressFn
from app.sources.base import SourceError, SourceNotFound, SourceRateLimited
from app.storage.protocol import Repo


def _noop(_: str) -> None:  # pragma: no cover - default progress sink
    pass


def _make_set_stage(repo: Repo, job_id: str | None, progress: ProgressFn) -> Callable[..., None]:
    """Build the progress callback shared by the pipelines: log the stage and,
    when a ``job_id`` is given, write status/detail (and optionally txs_done) to
    the jobs table for UI polling. ``updated_at`` is server-stamped on each write."""

    def set_stage(status: str, detail: str = "", *, txs_done: int | None = None) -> None:
        progress(f"[{status}] {detail}".rstrip())
        if job_id is not None:
            changes: dict[str, Any] = {"status": status, "stage_detail": detail}
            if txs_done is not None:
                changes["txs_done"] = txs_done
            repo.update_job(job_id, **changes)

    return set_stage


def _raise_if_incomplete(result: IngestResult) -> None:
    """Guard the pipelines against treating a partial download as success.

    ``ingest()`` returns ``rate_limited`` (cursor saved ``done=False``) when the
    source's quota stops the walk before it reaches the end. Clustering / scoring
    / classifying on that partial slice — and then marking the contract or job
    ``done`` — would freeze a model on non-representative data and report a false
    "done". Re-raise so the pipeline's ``except`` records a ``failed`` job with a
    clear message; a later re-run resumes from the saved cursor. ``completed`` and
    ``max_reached`` are both fully-ingested outcomes that proceed normally."""
    if result.status == "rate_limited":
        raise SourceRateLimited(
            "data provider request limit reached mid-download; "
            "partial data saved — re-run to resume"
        )


def _safe_error(exc: Exception) -> str:
    """A concise, client-safe error string for the jobs table — never the raw
    upstream response body (which is logged server-side instead)."""
    if isinstance(exc, SourceNotFound):
        return "address or policy id not found on-chain"
    if isinstance(exc, SourceRateLimited):
        # The cursor is saved on a rate limit, so retrying resumes where it
        # stopped (or restarts harmlessly if it hit the limit before any page).
        return "data provider request limit reached; progress saved — retry to resume"
    if isinstance(exc, SourceError):
        return "upstream data provider error; see server logs"
    return f"{type(exc).__name__}; see server logs"


# process_contract tunables. The DBSCAN fallbacks are the evaluator's own
# canonical values (imported above from app.clustering.evaluate, which reads
# config/clustering.yaml): FALLBACK_EPS when neither the grid recommendation
# nor the k-distance knee is available, MIN_SAMPLES_FLOOR when the grid has no
# recommendation.
_MIN_TXS_FOR_ANALYSIS = MIN_POINTS  # evaluate()'s own floor; below it we skip cluster/anomaly
_MAX_ERROR_DETAIL = 500  # cap the error string persisted to the jobs table
_CLASSIFY_BATCH = 1000  # online-score chunk size (bounds the IN(...) array + matrix)


def target_in_jobs(jobs: list[dict[str, Any]], target: str) -> bool:
    """Whether ``target`` has a non-terminal job in the already-fetched ``jobs``
    list. Takes the list (not the repo) so callers fetch nonterminal_jobs() once
    and the busy check reads the same across the API and the feed scheduler."""
    return any(j["target"] == target for j in jobs)


def _recommended_params(ev: dict[str, Any]) -> tuple[float, int]:
    """Pick DBSCAN ``(eps, min_samples)`` from an evaluation report, preferring the
    grid recommendation, then the k-distance knee, then heuristic fallbacks."""
    rec = ev.get("recommended") or {}
    eps = rec.get("eps") or (ev["k_distance"]["knee_eps"] or FALLBACK_EPS)
    min_samples = rec.get("min_samples") or MIN_SAMPLES_FLOOR
    return float(eps), int(min_samples)


def load_clustering_input(repo: Repo, target: str, feature_set: str) -> Any:
    shape_df = repo.fetch_shape_features(target) if feature_set in ("shape", "combined") else None
    addr_df = repo.fetch_tx_addresses(target) if feature_set in ("graph", "combined") else None
    return build_features(
        feature_set, shape_df, addr_df, max_graph_txs=get_settings().max_graph_txs
    )
