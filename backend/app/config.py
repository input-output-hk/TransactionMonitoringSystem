"""Configuration management using Pydantic Settings"""

import logging
import os
import re
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

logger = logging.getLogger(__name__)

# TMS_ENV must look like a bare identifier so it cannot traverse out of the
# project directory via the .env.<name> path composition below.
_TMS_ENV_RE = re.compile(r"[a-z0-9_-]+")


def _env_files() -> list[str]:
    """Select which dotenv file(s) to load.

    ``TMS_ENV=<name>`` picks which per-network file to layer on top of the
    shared ``.env`` (for example ``TMS_ENV=preview`` loads ``.env.preview``).
    Unset or empty ``TMS_ENV`` defaults to ``preprod``.

    Resolution paths cover both the backend working directory and the
    parent (project root), so launching uvicorn from either location works.

    Pydantic-settings loads the list left-to-right; **later files override
    earlier ones**. The shared ``.env`` is therefore listed first and the
    network-specific file last. Shell environment variables still win over
    every file.
    """
    raw = os.environ.get("TMS_ENV", "").strip() or "preprod"
    if not _TMS_ENV_RE.fullmatch(raw):
        raise RuntimeError(
            f"TMS_ENV must match [a-z0-9_-]+, got: {raw!r}"
        )
    specific = f".env.{raw}"
    candidates = [".env", "../.env", specific, f"../{specific}"]
    found = [p for p in candidates if Path(p).is_file()]
    logger.info(
        f"Config layering [TMS_ENV={raw}]: "
        f"loaded={found or ['<none>']} (later files override earlier)"
    )
    return candidates


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Cardano Network Configuration
    CARDANO_NETWORK: str = "mainnet"  # mainnet, preprod, preview, or testnet

    # Ogmios Configuration
    OGMIOS_WS_URL: str = "ws://localhost:1337"
    OGMIOS_RECONNECT_MAX_DELAY: int = 60  # max backoff delay in seconds
    OGMIOS_HEARTBEAT_INTERVAL: int = 30  # ping interval in seconds
    OGMIOS_HEARTBEAT_TIMEOUT: int = 90  # pong timeout in seconds
    OGMIOS_CIRCUIT_BREAKER_THRESHOLD: int = 5  # failures before cooldown
    OGMIOS_CIRCUIT_BREAKER_COOLDOWN: int = 120  # cooldown in seconds

    # API Server Configuration
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_TITLE: str = "Cardano Transaction Monitoring System"
    API_VERSION: str = "0.1.0"

    # Database Configuration - PostgreSQL
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "tms_user"
    POSTGRES_PASSWORD: str = "tms_password"
    POSTGRES_DB: str = "tms_db"

    # Database Configuration - ClickHouse
    CLICKHOUSE_HOST: str = "localhost"
    CLICKHOUSE_PORT: int = 9000
    CLICKHOUSE_HTTP_PORT: int = 8123
    CLICKHOUSE_USER: str = "default"
    CLICKHOUSE_PASSWORD: str = ""
    CLICKHOUSE_DB: str = "tms_analytics"

    # Security - API Key Authentication
    API_KEYS: str = ""  # Comma-separated list of valid API keys
    API_KEY_HEADER: str = "TMS-API-Key"  # Header name for API key
    # Must be "1" to allow the app to start with empty API_KEYS (dev mode).
    # Refusal-to-start otherwise prevents accidental open-API production deploys.
    TMS_ALLOW_DEV_MODE: str = ""
    # Override the directory pydantic-settings searches for detection.yaml.
    # Empty = use the default (project root's config/ dir, resolved upward).
    TMS_CONFIG_DIR: str = ""
    # Enable uvicorn's file-watch reloader (dev only; do not enable in Docker
    # or production — adds latency and spawns a watchdog subprocess).
    UVICORN_RELOAD: bool = False

    # Rate Limiting
    # Budget sized for an admin UI: the frontend dashboard polls ~5 widgets,
    # plus ad-hoc clicks (filters, archive ops, detail navigation). 240/min
    # = 4 req/sec sustained leaves comfortable headroom; abuse protection
    # is still meaningful at that ceiling.
    # Scope note: this flag gates the HTTP middleware and the WS handshake
    # check only. The per-email magic-link throttle (app.api.auth) is
    # always on, and limiter eviction loops run for every constructed
    # limiter regardless of this flag.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_REQUESTS: int = 240  # max requests per window per key/IP
    RATE_LIMIT_WINDOW_SECONDS: int = 60  # sliding window duration in seconds
    # Honour forwarded headers for client IPs (rate limiting, audit logs).
    # ONLY enable behind a reverse proxy / tunnel; the parsing rules below
    # (app.net.client_ip) take the right-most untrusted hop, never the
    # client-writable left entries.
    TRUSTED_PROXY_ENABLED: bool = False
    # Number of trusted proxies that append entries to X-Forwarded-For. The
    # client IP is taken HOPS entries from the RIGHT of the merged list; the
    # leftmost entries are attacker-writable and must never win. ge=1: zero
    # or negative hops would index past the list end on every request.
    TRUSTED_PROXY_HOPS: int = Field(default=1, ge=1)
    # Forwarded headers are honoured ONLY when the direct TCP peer falls
    # inside one of these CIDRs (the proxy itself). Defaults cover loopback
    # and the RFC1918 ranges Docker bridge networks use (cloudflared reaches
    # the app via the published loopback port / compose bridge gateway).
    TRUSTED_PROXY_CIDRS: str = (
        "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    )
    # Optional single-value client-IP header set by the edge (e.g.
    # "CF-Connecting-IP" for Cloudflare). Empty = use X-Forwarded-For.
    TRUSTED_PROXY_CLIENT_IP_HEADER: str = ""

    @property
    def trusted_proxy_networks(self) -> list:
        import ipaddress

        return [
            ipaddress.ip_network(c.strip(), strict=False)
            for c in self.TRUSTED_PROXY_CIDRS.split(",")
            if c.strip()
        ]

    # Comma-separated allowed CORS origins. "*" (default) keeps the demo
    # SPA and local vite dev servers working; tighten to the dashboard
    # origin in production.
    CORS_ALLOW_ORIGINS: str = "*"

    # /docs, /redoc, /openapi.json enumerate the whole admin attack surface
    # and are rate-limit exempt; always on in dev mode, opt-in on keyed
    # deployments.
    TMS_API_DOCS_ENABLED: bool = False

    @property
    def cors_allow_origins_list(self) -> list:
        return [o.strip() for o in self.CORS_ALLOW_ORIGINS.split(",") if o.strip()]

    # Analysis Engine
    ANALYSIS_ENGINE_ENABLED: bool = True
    ANALYSIS_ENGINE_INTERVAL_SECONDS: int = 30   # how often the engine polls for new txs
    ANALYSIS_ENGINE_BATCH_SIZE: int = 100         # max transactions scored per run
    # Cap on referenced-tx raw_data lookups per enrichment batch (bounds the
    # IN-clause size); inputs past the cap simply stay unresolved for the run.
    ANALYSIS_MAX_REF_TXS: int = 2000
    # Drain loop: batches pulled per interval tick while the poll comes back
    # full. Caps per-tick work so a deep backlog cannot monopolise the shared
    # ClickHouse executor; 20 x batch=100 clears 2000 txs/tick.
    ANALYSIS_ENGINE_MAX_BATCHES_PER_TICK: int = 20
    # Pause between drained batches: lets ingestion inserts and API reads
    # interleave on the 3-worker ClickHouse executor.
    ANALYSIS_ENGINE_DRAIN_SLEEP_SECONDS: float = 0.5
    # Watermark cursor for the unanalyzed poll. The overlap absorbs
    # same-second ordering skew and the tx-row-before-inputs-row insert gap;
    # the periodic full rescan (since=None) is the never-skip guarantee and
    # the recovery path for raw-data-deferred transactions.
    UNANALYZED_OVERLAP_SECONDS: int = 120
    UNANALYZED_FULL_RESCAN_INTERVAL_SECONDS: int = 600
    # Baseline lookup cache (0 disables). Baselines change once per daily
    # recompute but were point-SELECTed per feature per scored tx: the
    # engine's dominant N+1. insert_baselines() clears the cache; the 1 h
    # TTL bounds staleness from any out-of-band write to 4% of the cadence.
    BASELINE_CACHE_TTL_SECONDS: int = 3600
    # ~500 scripts x 8 features with generous headroom; overflow clears.
    BASELINE_CACHE_MAX_ENTRIES: int = 50_000
    # Token-registry refresh cadence (background task only; the scoring
    # path never fetches). Matches the registry cache TTL.
    TOKEN_REGISTRY_REFRESH_INTERVAL_HOURS: int = 24

    # WebSocket feed. Per-client outbound queue depth: a client lagging by
    # more than this many events starts losing the OLDEST ones (the feed is
    # a live view, not the system of record). Connection cap prevents
    # resource exhaustion.
    WS_CLIENT_QUEUE_SIZE: int = 100
    WS_MAX_CONNECTIONS: int = 100
    # WS handshake attempts per client IP per window. Connections are
    # long-lived, so a legitimate dashboard needs ~1/min even with flaky
    # networking; 30/min absorbs aggressive reconnect loops without letting
    # rejected upgrades churn unthrottled.
    WS_HANDSHAKE_RATE_LIMIT_REQUESTS: int = 30
    WS_HANDSHAKE_RATE_LIMIT_WINDOW_SECONDS: int = 60

    # Ogmios frames larger than this parse on a worker thread instead of
    # the event loop (a busy Plutus block serialises to tens of MB and
    # would otherwise freeze the API/WS/mempool tasks for the parse).
    # Below it the thread handoff costs more than the parse itself.
    OGMIOS_PARSE_EXECUTOR_THRESHOLD_BYTES: int = 1_048_576  # 1 MiB

    # Retention. ALL default 0 = keep forever (the audit's growth findings
    # are addressed by giving operators knobs, not by silently expiring
    # data). tx_class_scores / archived_alerts / baselines are never
    # expired regardless of these settings.
    CH_RETENTION_DAYS_TRANSACTIONS: int = 0
    CH_RETENTION_DAYS_IO: int = 0          # inputs / outputs / address_transactions
    CH_RETENTION_DAYS_FEATURES: int = 0    # utxo_features / tx_script_features
    LIFECYCLE_RETENTION_DAYS: int = 0      # terminal (DROPPED/ROLLED_BACK) rows only
    MEMPOOL_COLLISION_RETENTION_DAYS: int = 0
    # Audit rows are the suppression accountability record: prefer archiving
    # the table over short retention.
    AUDIT_LOG_RETENTION_DAYS: int = 0
    # Raw-store day-directory pruning. Refused while RAW_DATA_MAX_BYTES > 0:
    # capped ClickHouse payloads make the raw store load-bearing for the
    # engine's raw_data fallback.
    RAW_STORE_RETENTION_DAYS: int = 0
    RETENTION_SWEEP_INTERVAL_HOURS: int = 24

    # Background-task supervisor restart backoff. Base doubles per crash up
    # to the ceiling; a run lasting longer than the stable-reset window
    # resets the delay (one-off crashes recover fast, persistent bugs stop
    # hammering logs and downstream services at a fixed cadence).
    SUPERVISOR_BACKOFF_BASE_SECONDS: float = 5.0
    SUPERVISOR_BACKOFF_MAX_SECONDS: float = 300.0
    SUPERVISOR_STABLE_RESET_SECONDS: float = 600.0

    # Analysis Engine: multi-class detection
    ANALYSIS_ENABLED: bool = True
    BASELINE_MIN_SAMPLES: int = 200          # min txs before per-script baseline is trusted
    BASELINE_BOOTSTRAP_ON_STARTUP: bool = True
    BASELINE_RECOMPUTE_INTERVAL_HOURS: int = 24  # recompute script baselines daily
    BASELINE_MAX_SCRIPTS: int = 500              # max script addresses to recompute per cycle
    SCORER_PHISHING_ENABLED: bool = True
    SCORER_TOKEN_DUST_ENABLED: bool = True
    SCORER_LARGE_VALUE_ENABLED: bool = True
    SCORER_LARGE_DATUM_ENABLED: bool = True
    SCORER_MULTIPLE_SAT_ENABLED: bool = True
    SCORER_FAKE_TOKEN_ENABLED: bool = True
    SCORER_FRONT_RUNNING_ENABLED: bool = True
    SCORER_SANDWICH_ENABLED: bool = True
    SCORER_CIRCULAR_ENABLED: bool = True

    # Fake-token testnet mode: enables the mainnet-curated legitimate-token
    # registry on preprod / preview networks. Intended ONLY for running the
    # internal/attacks.py harness against a test deployment; keep False in
    # production. Rationale:
    #   - The registry lists mainnet policy IDs for HOSKY, iUSD, DJED, etc.
    #   - Dev/test workflows on testnets mint tokens with the same names
    #     (e.g. for dApp integration tests). Under this flag every such mint
    #     fires the fake_token scorer, producing a guaranteed stream of
    #     false positives unrelated to any real threat.
    #   - When you need to verify detector coverage against build_fake_token,
    #     flip this on, run the harness, observe the detection, flip it off.
    FAKE_TOKEN_TESTNET_MODE: bool = False

    # Phase 4: cross-transaction detection infrastructure
    COLLISION_DETECTION_ENABLED: bool = True
    CYCLE_DETECTION_ENABLED: bool = True
    CYCLE_MAX_HOPS: int = 6
    CYCLE_MAX_FANOUT: int = 50
    SANDWICH_SIMPLIFIED_ENABLED: bool = True

    # ClickHouse write resilience.
    # A block whose ClickHouse insert fails must NOT have its sync checkpoint
    # advanced (silent permanent block loss). The ingester retries the insert
    # with exponential backoff, then raises BlockPersistError so the chain-sync
    # loop trips its circuit breaker and replays the block from the unadvanced
    # checkpoint (safe: all fact tables are ReplacingMergeTree).
    CLICKHOUSE_INSERT_MAX_RETRIES: int = 5  # total attempts before giving up
    # First retry delay; doubles per attempt. 1 s rides out a ClickHouse
    # merge stall / brief socket drop without flooding reconnects.
    CLICKHOUSE_INSERT_RETRY_BASE_DELAY_SECONDS: float = 1.0
    # Backoff ceiling: a ClickHouse restart takes tens of seconds; waiting
    # longer than 30 s per attempt just delays the circuit-breaker handoff.
    CLICKHOUSE_INSERT_RETRY_MAX_DELAY_SECONDS: float = 30.0

    # Chain rollback cleanup: on rollBackward, delete ClickHouse rows for
    # transactions whose slot is past the rollback point so orphaned-fork data
    # cannot feed scorers or API reads. archived_alerts is exempt (admin
    # curation, not chain state).
    ROLLBACK_CLEANUP_ENABLED: bool = True
    # Second rollback purge pass for tx_class_scores. The first purge can
    # race an in-flight engine batch (run_once holds its fetched rows for
    # seconds and inserts scores at the end), leaving a stale score row that
    # the unanalyzed anti-join treats as "already scored" forever. 60 s is
    # comfortably longer than one batch's wall time; deleting a fresh score
    # of a re-confirmed tx is recall-safe (the engine simply re-scores it).
    ROLLBACK_SCORE_REPURGE_DELAY_SECONDS: int = 60

    # Maximum byte-length of the raw_data JSON stored per transaction.
    # 0 = no limit (store the full payload; ZSTD codec keeps it cheap).
    # When > 0 and the serialized JSON exceeds the limit, an EMPTY string is
    # stored with raw_data_truncated = 1 — never an invalid JSON prefix, which
    # previously made the scorer silently treat the tx as feature-less.
    RAW_DATA_MAX_BYTES: int = 0

    # Analysis Engine raw_data fallback: when a tx row's raw_data is missing,
    # truncated, or unparseable, read the full payload back from the raw store
    # (ADR-009) before scoring. On a failed read the tx is deferred (no score
    # row written) and retried on later engine runs.
    RAW_FALLBACK_ENABLED: bool = True
    # After this many failed fallback attempts the tx is scored anyway with
    # raw_data=None and a raw_data_unavailable evidence marker, so a lost blob
    # cannot park a tx in the pending queue forever. 3 covers transient
    # filesystem hiccups across the paced retry budget below.
    RAW_FALLBACK_MAX_ATTEMPTS: int = 3
    # Minimum monotonic-clock spacing between COUNTED fallback attempts
    # (time.monotonic(), immune to NTP steps). The drain loop re-polls every
    # ANALYSIS_ENGINE_DRAIN_SLEEP_SECONDS (0.5 s) under load, which burned
    # the whole attempt budget in ~1.5 s instead of the intended
    # one-attempt-per-engine-interval; 30 s matches
    # ANALYSIS_ENGINE_INTERVAL_SECONDS. With MAX_ATTEMPTS=3 there are two
    # paced gaps between counted attempts, so the degrade budget floor is
    # at least 60 s after the first failure (recovery is still probed on
    # every poll regardless).
    RAW_FALLBACK_RETRY_SECONDS: int = 30

    # Mempool pending-tx bookkeeping. The TTL matches
    # LIFECYCLE_PENDING_TTL_SECONDS: both bound how long an unconfirmed tx
    # stays relevant (on-chain tx TTLs cover user submissions well inside 2 h).
    MEMPOOL_PENDING_TTL_SECONDS: int = 7200
    # Prune cadence: amortizes the O(pending) stale scan to once per N
    # processed mempool txs instead of per tx. ge=1: 0 would divide by zero
    # inside the mempool loop, where the exception is swallowed at DEBUG.
    MEMPOOL_PRUNE_EVERY_N_TXS: int = Field(default=100, ge=1)
    # Hard cap on the seen-tx dedup set; clearing it only risks re-processing
    # (idempotent downstream), never data loss.
    MEMPOOL_SEEN_TXS_MAX: int = 50_000

    # In-process TTL for the dashboard stats aggregate (full-table FINAL scan
    # + countDistinct per call). The dashboard polls ~every 15 s; 10 s bounds
    # the scan rate to ~1 per poll cycle however many dashboards are open.
    # 0 disables. Staleness only affects KPI cards, never detection.
    STATS_CACHE_TTL_SECONDS: int = 10

    # Lifecycle cleanup — PENDING → DROPPED sweep
    # PENDING transactions older than this threshold are marked DROPPED by the
    # background cleanup sweep that runs alongside the analysis engine.
    # Cardano transactions carry an on-chain TTL; 7200 s (2 h) covers most
    # user-submitted transactions on Preprod and Mainnet.
    LIFECYCLE_PENDING_TTL_SECONDS: int = 7200

    # Raw Transaction Store — local filesystem blob storage (ADR-009)
    # Full Ogmios JSON per transaction, gzip-compressed.
    # Path: {RAW_STORE_PATH}/{confirmed|mempool}/{network}/{YYYYMMDD}/{tx_hash[:2]}/{tx_hash}.json.gz
    # Upgrade path: MinIO (production) → S3/R2/B2 (Mainnet).
    RAW_STORE_PATH: str = "./data/raw"
    RAW_STORE_ENABLED: bool = True

    # ── Magic-link auth ───────────────────────────────────────────────────
    # Sessions live in `user_sessions`. Cookie name and
    # TTL are tunable; defaults match the design doc.
    SESSION_COOKIE_NAME: str = "tms_session"
    SESSION_TTL_DAYS: int = 7
    # Magic-link tokens are short-lived. 15 min keeps the interception
    # window narrow while leaving slack for slow mail delivery.
    MAGIC_LINK_TTL_MINUTES: int = 15
    # Ceiling on how many `/api/auth/verify` calls the same token can
    # survive before being forcibly marked consumed. Set to 3 by default
    # as a safety buffer for naive email-scanner pre-fetches and quick
    # retries (network blip, double-click).
    #
    # IMPORTANT: this is NOT the everyday behaviour. The token is
    # **also** marked consumed the first time the resulting session is
    # used on any authenticated endpoint (via `claim_session_token`).
    # So in the happy path — user clicks link, browser establishes
    # session, AuthProvider hits /me — the token dies on that /me, even
    # if the counter still shows 2 redemptions left. The counter is the
    # emergency hatch, the back-reference claim is the normal one.
    MAGIC_LINK_MAX_REDEMPTIONS: int = 3
    # Per-email throttle on /api/auth/request-link. Caps how many fresh
    # tokens a single address can request in the window.
    MAGIC_LINK_PER_EMAIL_LIMIT: int = 5
    MAGIC_LINK_PER_EMAIL_WINDOW_SECONDS: int = 15 * 60  # 15 minutes
    # Used to build the verification URL inside outgoing magic-link emails.
    # In dev this is the local app, in prod the public host behind the tunnel.
    APP_BASE_URL: str = "http://localhost:8000"

    # ── SMTP (outgoing magic-link emails) ─────────────────────────────────
    # In dev we run Mailpit on the docker-compose network (`mailpit:1025`,
    # web UI on 8025). In prod the customer supplies their own server via
    # these env vars — no code changes needed.
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 1025
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = False  # implicit TLS (port 465 style)
    SMTP_USE_STARTTLS: bool = False  # opportunistic STARTTLS upgrade
    SMTP_FROM_EMAIL: str = "noreply@tms.local"
    SMTP_FROM_NAME: str = "TMS"
    # When SMTP_HOST is unset/empty we log the magic link instead of sending
    # an email — useful for tests and bootstrap before SMTP is configured.
    SMTP_ENABLED: bool = True

    # Logging
    LOG_LEVEL: str = "INFO"

    # Files are loaded in order with later files overriding earlier ones (see
    # _env_files docstring). The shared `.env` holds cross-network defaults;
    # `.env.<TMS_ENV>` supplies per-network overrides for CARDANO_NETWORK,
    # OGMIOS_WS_URL, API_PORT.
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


# Global settings instance
settings = Settings()
