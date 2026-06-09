"""Configuration management using Pydantic Settings"""

import logging
import os
import re
from pathlib import Path

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
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_REQUESTS: int = 240  # max requests per window per key/IP
    RATE_LIMIT_WINDOW_SECONDS: int = 60  # sliding window duration in seconds

    # Analysis Engine
    ANALYSIS_ENGINE_ENABLED: bool = True
    ANALYSIS_ENGINE_INTERVAL_SECONDS: int = 30   # how often the engine polls for new txs
    ANALYSIS_ENGINE_BATCH_SIZE: int = 100         # max transactions scored per run

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
