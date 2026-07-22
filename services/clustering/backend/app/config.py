"""Application configuration, loaded from environment variables."""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Blockfrost base URLs per Cardano network (used only when CHAIN_SOURCE=blockfrost;
# the host_ch default reads ClickHouse and never touches these).
_BLOCKFROST_BASE_URLS = {
    "mainnet": "https://cardano-mainnet.blockfrost.io/api/v0",
    "preprod": "https://cardano-preprod.blockfrost.io/api/v0",
    "preview": "https://cardano-preview.blockfrost.io/api/v0",
}


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
    # keys both the host_ch reads and the published contract_anomaly rows. Also
    # selects the Blockfrost base URL when CHAIN_SOURCE=blockfrost.
    cardano_network: str = Field(default="mainnet", alias="CARDANO_NETWORK")

    # Blockfrost (only consumed when CHAIN_SOURCE=blockfrost; the host_ch default
    # ignores all of these). Project id authenticates every request; empty is only
    # valid for host_ch runs.
    blockfrost_project_id: str = Field(default="", alias="BLOCKFROST_PROJECT_ID")
    # Free tier: 10 req/s sustained, burst of 500 refilling at 10/s.
    blockfrost_max_rps: float = Field(default=10.0, alias="BLOCKFROST_MAX_RPS")
    blockfrost_burst: int = Field(default=500, alias="BLOCKFROST_BURST")
    blockfrost_page_size: int = Field(default=100, alias="BLOCKFROST_PAGE_SIZE")
    # Per-request HTTP timeout, max retry attempts for transient errors (429/5xx/
    # transport), and the ceiling on exponential backoff between those retries.
    blockfrost_timeout_s: float = Field(default=30.0, alias="BLOCKFROST_TIMEOUT_S")
    blockfrost_max_retries: int = Field(default=6, alias="BLOCKFROST_MAX_RETRIES")
    blockfrost_backoff_cap_s: float = Field(default=30.0, alias="BLOCKFROST_BACKOFF_CAP_S")

    # Upper bound on transactions fed to the O(n^2) precomputed-Jaccard graph
    # clustering. Above this the tx set is sampled (and the drop is logged) to
    # avoid an unbounded n x n distance-matrix allocation.
    max_graph_txs: int = Field(default=5000, alias="MAX_GRAPH_TXS")

    # Sample cap for the silhouette quality score. Exact silhouette is O(n^2)
    # in the scored points and runs once per grid config at every fit and
    # evaluation, so an uncapped 50k-tx window (clustering_window_txs) would
    # cost ~2.5e9 pairwise distances per config, times ~18 grid configs. Above
    # this many non-noise points the score is estimated on a fixed-seed
    # subsample (sklearn's sample_size): 2000 points keep each evaluation at
    # ~4e6 distances while the sampling error stays far below the score gaps
    # the parameter recommender discriminates. 0 disables sampling (exact
    # score; small windows and tests).
    silhouette_sample_size: int = Field(default=2000, ge=0, alias="SILHOUETTE_SAMPLE_SIZE")

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
    # Query ceilings (defense-in-depth; both abort with a VISIBLE error, never a
    # silent truncation — a failed run is loud and retryable, so this does not
    # drop transactions from analysis the way max_result_rows / a short
    # max_execution_time would, which is why those are deliberately NOT set).
    # send_receive_timeout: socket ceiling for a wedged query. Generous so a
    # legitimately large windowed read never trips it. Same env name as the
    # host backend, but the default is per-driver: the host's clickhouse-driver
    # (native protocol) streams packets, so its 120s bounds a wedged socket
    # well below the driver default; this service's clickhouse-connect (HTTP)
    # returns no bytes until the query finishes, so a long legitimate fit needs
    # the full 300. Compose passes each service its own CLUSTERING_CLICKHOUSE_*
    # knob so the shared name never forces a shared value.
    clickhouse_send_receive_timeout_seconds: float = Field(
        default=300.0,
        ge=1,
        alias="CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS",
    )
    # connect_timeout: TCP connection-establishment ceiling, mirroring the host
    # backend's name/type/default so a partition without an RST fails fast
    # instead of hanging a worker on connect.
    clickhouse_connect_timeout_seconds: float = Field(
        default=10.0,
        ge=1,
        alias="CLICKHOUSE_CONNECT_TIMEOUT_SECONDS",
    )
    # max_memory_usage: per-query server memory cap. 0 = unset (use the server
    # default). Sized below the container mem_limit so a runaway query aborts
    # instead of OOM-killing the ClickHouse container.
    clickhouse_max_memory_usage_bytes: int = Field(
        default=0,
        ge=0,
        alias="CLICKHOUSE_MAX_MEMORY_USAGE_BYTES",
    )

    # Host-backed integration (CHAIN_SOURCE=host_ch). When the engine runs as
    # the TMS clustering sidecar it reads each watched contract's transactions
    # directly from the host TMS's analytics database on the SAME ClickHouse
    # server (no external provider, no raw-tx duplication): engine-owned state is
    # written to ``clickhouse_db`` (tms_clustering); raw tx/feature reads come
    # from ``host_clickhouse_db`` (tms_analytics) via the HostBackedRepo.
    host_clickhouse_db: str = Field(default="tms_analytics", alias="HOST_CLICKHOUSE_DB")
    # Rolling-window CEILING on the fit/classify population: no watched contract
    # is ever clustered/scored on more than the most recent N transactions, so
    # DBSCAN + IsolationForest + the O(n^2) silhouette stay bounded for a
    # high-volume mainnet contract (the ceiling is also the sidecar's hard memory
    # bound). A contract's ACTUAL window is its per-contract "latest N to cluster
    # on" (requested_max_txs), clamped to this ceiling; a contract that carries
    # none falls back to the ceiling itself. See effective_window_txs().
    # 0 = unbounded (small/test contracts only; never for mainnet).
    clustering_window_txs: int = Field(default=50_000, alias="CLUSTERING_WINDOW_TXS")
    # Recall floor on the per-contract "latest N": an operator's N is clamped UP
    # to at least this many transactions before it bounds the fit. The detectors
    # need a baseline to call an outlier against — LOF reads a fixed
    # anomaly.lof_neighbors (20) neighborhood and DBSCAN a min_samples grid, so
    # below a few multiples of those a genuine anomaly hides in a too-thin sample
    # instead of standing out (recall first: when N is uncertain, keep MORE
    # data). Only clamps an explicit N > 0; an unset contract uses the ceiling.
    # The default is ~10x lof_neighbors, comfortably above the min_samples grid.
    clustering_min_target_txs: int = Field(default=200, ge=1, alias="CLUSTERING_MIN_TARGET_TXS")
    # The "latest N to cluster on" a contract is onboarded with when the operator
    # names no explicit N (the API/UI onboard path; the feed's refit jobs carry 0
    # and preserve the persisted value instead). Applied server-side so every
    # onboard client defaults the same way. Sits at the backfill ceiling
    # (history_max_txs_ceiling) so a fresh contract can fully populate its window
    # from the history source; keep the two in step if you retune either.
    clustering_default_target_txs: int = Field(
        default=5_000, ge=1, alias="CLUSTERING_DEFAULT_TARGET_TXS"
    )
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
    # Contracts enqueued per tick: bounds per-tick work so a large watchlist
    # cannot flood the single job worker (mirrors the host's per-tick drain cap).
    feed_max_contracts_per_tick: int = Field(default=4, alias="FEED_MAX_CONTRACTS_PER_TICK")

    # Optional secondary HISTORY source (host_ch deployments only). The host
    # syncs tip-forward, so a watched contract's pre-deployment history never
    # reaches the host tables; when set, every onboarded contract automatically
    # backfills up to its per-contract cap from this source before its first
    # fit (see service/history.py). "" disables. "blockfrost" downloads into
    # the engine's own raw tables, read via the hybrid repo; "kupo" triggers
    # the host's own full-fidelity POST /api/v1/backfill (rows land in the
    # host tables, no sidecar-local writes).
    history_source: str = Field(default="", alias="HISTORY_SOURCE")
    # Per-contract history depth when the contract row carries no
    # requested_max_txs (host-backed feed onboarding leaves it 0). 500 mirrors
    # the host's BACKFILL_DEFAULT_MAX_TXS so both flavors default to the same
    # depth.
    history_max_txs: int = Field(default=500, ge=1, alias="HISTORY_MAX_TXS")
    # Clamp on per-contract overrides (the UI re-exposes "max txs" when history
    # is enabled); mirrors the host's BACKFILL_MAX_TXS_CAP so neither flavor
    # can request more history than the host's own backfill endpoint allows.
    history_max_txs_ceiling: int = Field(default=5000, ge=1, alias="HISTORY_MAX_TXS_CEILING")
    # Shortfall gate for the backfill (blockfrost flavor only, like the
    # window-full pre-flight below; the kupo flavor computes no boundary and so
    # honors neither). When > 0, skip the pre-deployment top-up for any target
    # the host ALREADY holds at least this many rows for: a large recent host
    # sample anchors the fit on its own, so older history buys little and is not
    # worth the provider quota. 0 (default) disables the gate and preserves the
    # always-top-up behavior; the window-full pre-flight (host_tx_count >=
    # clustering_window_txs) still applies regardless. Only values below
    # clustering_window_txs have any effect. NOTE: a skipped contract is marked
    # done (skip-fast), so LOWERING this threshold later does NOT re-open its
    # backfill; raise the per-contract cap to re-open (mirrors the window marker).
    history_min_host_txs: int = Field(default=0, ge=0, alias="HISTORY_MIN_HOST_TXS")
    # Host API base URL and credential, consumed only by HISTORY_SOURCE=kupo:
    # the sidecar triggers the host's backfill over the compose network (the
    # host additionally needs its own KUPO_URL configured).
    host_api_url: str = Field(default="", alias="HOST_API_URL")
    host_api_key: str = Field(default="", alias="HOST_API_KEY")
    # Per-request ceiling on the host-API round trip (trigger POST / status
    # GET). The POST returns 202 immediately (the host runs the scan in its own
    # background task), so this bounds a wedged connection, not the backfill.
    host_api_timeout_seconds: float = Field(default=30.0, ge=1, alias="HOST_API_TIMEOUT_SECONDS")

    # API security / ops. All optional so local/demo runs stay zero-config; set
    # API_KEY and CORS_ORIGINS to lock down a network-exposed deployment.
    api_key: str = Field(default="", alias="API_KEY")
    # Comma-separated HMAC keys for stored model blobs: sign with the first,
    # verify against any (rotation). Empty = unsigned blobs (local demo only);
    # REQUIRED in production — a tampered blob is pickle, i.e. code execution.
    model_signing_keys: str = Field(default="", alias="MODEL_SIGNING_KEYS")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")  # comma-separated
    # Production safety switch. When true, startup refuses to boot unless both
    # API_KEY and MODEL_SIGNING_KEYS are set (an empty API_KEY makes auth a
    # no-op, and unsigned model blobs are pickle = code execution on load).
    # Default false so local/test/demo stay zero-config; compose sets it to 1
    # for any network-exposed deployment.
    require_auth: bool = Field(default=False, alias="REQUIRE_AUTH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # "json" = one structured object per line (aggregator-friendly; compose default),
    # "text" = human-readable (bare default, nicer for the CLI / local dev).
    log_format: str = Field(default="text", alias="LOG_FORMAT")
    # Reject new onboarding jobs once this many are already non-terminal (DoS /
    # paid-quota guard on the unauthenticated-by-default enqueue endpoint).
    max_inflight_jobs: int = Field(default=8, alias="MAX_INFLIGHT_JOBS")
    # Cap on CONCURRENT ad-hoc analysis runs (POST /anomaly, /cluster, GET
    # /evaluation). Each loads the full window + DBSCAN + an O(n^2) silhouette,
    # so unbounded concurrency would overload ClickHouse and the box. Excess
    # requests WAIT for a slot (they are not rejected — an analyst's run must
    # still complete). Keep small relative to the ClickHouse capacity.
    max_concurrent_analyses: int = Field(default=2, ge=1, alias="MAX_CONCURRENT_ANALYSES")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def host_backed(self) -> bool:
        """True when the engine reads each contract's txs from the host's tables
        (CHAIN_SOURCE=host_ch) instead of downloading them: no per-contract
        download, fits run over the rolling window. Single source of truth for
        the "host_ch" check so the many call sites can't drift."""
        return self.chain_source == "host_ch"

    @property
    def history_enabled(self) -> bool:
        """True when a secondary source backfills watched contracts'
        pre-deployment history. Only meaningful under host_ch (the blockfrost
        primary already downloads history itself; startup rejects the combo).
        Single source of truth, same rationale as ``host_backed`` above."""
        return self.host_backed and bool(self.history_source)

    @property
    def blockfrost_base_url(self) -> str:
        try:
            return _BLOCKFROST_BASE_URLS[self.cardano_network]
        except KeyError as exc:  # pragma: no cover - defensive
            raise ValueError(
                f"Unknown CARDANO_NETWORK {self.cardano_network!r}; "
                f"expected one of {sorted(_BLOCKFROST_BASE_URLS)}"
            ) from exc

    def effective_window_txs(self, requested_max_txs: int) -> int:
        """A contract's actual rolling window: the "latest N to cluster on" it
        was onboarded with (``requested_max_txs``), clamped to
        ``[clustering_min_target_txs, clustering_window_txs]``.

        The single definition of "how many recent transactions does THIS
        contract fit/score/count on", shared by the repo's read window
        (``_window_for``) and the backfill's window-full skip gate so the number
        the operator picked, the number the card shows, and the point past which
        older history is not worth fetching can never drift apart.

        ``requested_max_txs`` 0/unset falls back to the ceiling: legacy
        feed-onboarded rows carry none, and defaulting them to the full window
        keeps the recall-safe status quo (never silently shrinks an existing
        fit). A ceiling of 0 means "unbounded" (small/test contracts) and is
        returned as-is so the caller omits the LIMIT entirely."""
        ceiling = self.clustering_window_txs
        if ceiling <= 0:
            return 0
        if requested_max_txs <= 0:
            return ceiling
        floor = min(self.clustering_min_target_txs, ceiling)  # floor can't exceed the ceiling
        return max(floor, min(requested_max_txs, ceiling))

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
