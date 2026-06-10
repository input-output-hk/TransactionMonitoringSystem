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
    # Cap on referenced-tx raw_data lookups per enrichment batch (bounds the
    # IN-clause size); inputs past the cap simply stay unresolved for the run.
    ANALYSIS_MAX_REF_TXS: int = 2000

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
    # filesystem hiccups across ~3 engine intervals.
    RAW_FALLBACK_MAX_ATTEMPTS: int = 3

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
