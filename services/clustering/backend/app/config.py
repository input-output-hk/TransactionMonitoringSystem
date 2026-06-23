"""Application configuration, loaded from environment variables."""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration sourced from the environment / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Chain data source: which ChainSource adapter the pipeline/CLI use (see
    # app/sources/factory.py). The TMS deployment runs "host_ch" (reads the
    # TMS's own ingested chain data; the clustering compose service sets it
    # explicitly). The seam admits a future node/db-sync adapter behind the same
    # protocol without touching analysis code.
    chain_source: str = Field(default="host_ch", alias="CHAIN_SOURCE")

    # Cardano network the sidecar is pinned to; matches the host TMS's network and
    # keys both the host_ch reads and the published contract_anomaly rows.
    cardano_network: str = Field(default="mainnet", alias="CARDANO_NETWORK")

    # Upper bound on transactions fed to the O(n^2) precomputed-Jaccard graph
    # clustering. Above this the tx set is sampled (and the drop is logged) to
    # avoid an unbounded n x n distance-matrix allocation.
    max_graph_txs: int = Field(default=5000, alias="MAX_GRAPH_TXS")

    # Drift trigger for the online classifier. The "online-noise rate" is the
    # fraction of recently-classified txs that fall outside every frozen cluster
    # (cluster_id == -1). Above this the frozen model is treated as stale and the
    # UI suggests a full re-cluster. PSI-style banding: <0.15 healthy, 0.15-0.25
    # watch, >=0.25 re-cluster recommended. Re-cluster is never automatic.
    recluster_noise_threshold: float = Field(default=0.25, alias="RECLUSTER_NOISE_THRESHOLD")

    # Trailing sample size for the online-noise rate above: how many of the most
    # recently scored txs the drift sensor measures. Larger smooths the signal
    # (slower to react, steadier); smaller reacts faster to a distribution shift
    # but is noisier. A tunable, so it lives here beside its threshold rather
    # than as a literal in the storage query.
    online_noise_window: int = Field(default=500, alias="ONLINE_NOISE_WINDOW")

    # Number of transactions whose (tx, utxos) pairs are fetched concurrently
    # within a page during ingest. Admission is still serialized by the token
    # bucket, so this overlaps round-trip latency without exceeding the rate.
    ingest_concurrency: int = Field(default=8, alias="INGEST_CONCURRENCY")

    # Number of transactions buffered before a batched insert into ClickHouse.
    # Trades memory (rows held in RAM) against insert round-trips.
    ingest_batch_size: int = Field(default=200, alias="INGEST_BATCH_SIZE")

    # ClickHouse
    clickhouse_host: str = Field(default="localhost", alias="CLICKHOUSE_HOST")
    clickhouse_http_port: int = Field(default=8123, alias="CLICKHOUSE_HTTP_PORT")
    # Default matches the host's CLUSTERING_DB default (app/config.py) and the
    # docker-compose wiring, so a standalone run (no compose env) and the host's
    # read/purge paths agree on the database name. A mismatch here would make the
    # host silently read an empty DB (every cross-db read/purge is best-effort
    # and swallows the resulting UNKNOWN_DATABASE), masking the misconfiguration.
    clickhouse_db: str = Field(default="tms_clustering", alias="CLICKHOUSE_DB")
    clickhouse_user: str = Field(default="tms", alias="CLICKHOUSE_USER")
    clickhouse_password: str = Field(default="tms", alias="CLICKHOUSE_PASSWORD")

    # Host-backed integration (CHAIN_SOURCE=host_ch). When the engine runs as
    # the TMS clustering sidecar it reads each watched contract's transactions
    # directly from the host TMS's analytics database on the SAME ClickHouse
    # server (no external provider, no raw-tx duplication): engine-owned state is
    # written to ``clickhouse_db`` (tms_clustering); raw tx/feature reads come
    # from ``host_clickhouse_db`` (tms_analytics) via the HostBackedRepo.
    host_clickhouse_db: str = Field(default="tms_analytics", alias="HOST_CLICKHOUSE_DB")
    # Rolling-window bound on the fit/classify population: only the most recent
    # N transactions of a watched contract are clustered/scored, so DBSCAN +
    # IsolationForest + the O(n^2) silhouette stay bounded for a high-volume
    # mainnet contract (the window is also the sidecar's hard memory bound).
    # 0 = unbounded (small/test contracts only; never for mainnet).
    clustering_window_txs: int = Field(default=50_000, alias="CLUSTERING_WINDOW_TXS")
    # Default feature set for host-backed fits. "shape" scales to any volume;
    # "graph"/"combined" are O(n^2) and capped by max_graph_txs, so they are
    # opt-in per contract rather than the default at mainnet scale.
    clustering_default_feature_set: str = Field(
        default="shape", alias="CLUSTERING_DEFAULT_FEATURE_SET"
    )

    # Automatic feed (host_ch only). The scheduler polls the watchlist and
    # enqueues a classify job per non-busy contract (an onboard/refit when the
    # contract has no model yet or its drift crosses recluster_noise_threshold),
    # so a watched contract is scored automatically as the host ingests its new
    # transactions — no manual "fetch" step. Disable to run the engine API
    # without the background feed.
    feed_enabled: bool = Field(default=True, alias="FEED_ENABLED")
    feed_poll_interval_seconds: int = Field(default=30, alias="FEED_POLL_INTERVAL_SECONDS")
    # Re-fit a watched contract at least this often even without a drift trigger,
    # so a slowly-drifting frozen model is refreshed on a bounded (windowed) fit.
    feed_refit_max_age_seconds: int = Field(default=86_400, alias="FEED_REFIT_MAX_AGE_SECONDS")
    # Contracts enqueued per tick: bounds per-tick work so a large watchlist
    # cannot flood the single job worker (mirrors the host's per-tick drain cap).
    feed_max_contracts_per_tick: int = Field(default=4, alias="FEED_MAX_CONTRACTS_PER_TICK")

    # API security / ops. All optional so local/demo runs stay zero-config; set
    # API_KEY and CORS_ORIGINS to lock down a network-exposed deployment.
    api_key: str = Field(default="", alias="API_KEY")
    # Comma-separated HMAC keys for stored model blobs: sign with the first,
    # verify against any (rotation). Empty = unsigned blobs (local demo only);
    # REQUIRED in production — a tampered blob is pickle, i.e. code execution.
    model_signing_keys: str = Field(default="", alias="MODEL_SIGNING_KEYS")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")  # comma-separated
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # "json" = one structured object per line (aggregator-friendly; compose default),
    # "text" = human-readable (bare default, nicer for the CLI / local dev).
    log_format: str = Field(default="text", alias="LOG_FORMAT")
    # Reject new onboarding jobs once this many are already non-terminal (DoS /
    # paid-quota guard on the unauthenticated-by-default enqueue endpoint).
    max_inflight_jobs: int = Field(default=8, alias="MAX_INFLIGHT_JOBS")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def recluster_recommended(self, drift_score: float) -> bool:
        """Whether an online-classifier ``drift_score`` is stale enough to recommend
        a full re-cluster. Single source of truth for the threshold rule, shared by
        the API (``ContractOut.reclustering_suggested``) and the classify job's
        detail message — keep both reading this so they can't disagree."""
        return drift_score >= self.recluster_noise_threshold


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()


class _JsonFormatter(logging.Formatter):
    """One JSON object per line — greppable locally, parseable by any aggregator.
    stdlib-only on purpose (no log-library dependency for four fields)."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        entry: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Structured extras (e.g. the request-log middleware's fields).
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            entry.update(extra)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(settings: Settings | None = None) -> None:
    """Install the root logging config honoring ``LOG_LEVEL`` / ``LOG_FORMAT``.

    Idempotent and safe to call from both the FastAPI lifespan and the CLI so
    background-worker diagnostics aren't lost under the default handler.
    """
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if settings.log_format.strip().lower() == "json":
        formatter = _JsonFormatter()
        for handler in logging.getLogger().handlers:
            handler.setFormatter(formatter)
