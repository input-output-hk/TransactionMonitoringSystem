"""Configuration management using Pydantic Settings"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


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

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_REQUESTS: int = 60   # max requests per window per key/IP
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

    # Logging
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=[".env", "../.env"],  # Look in current dir and parent dir
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


# Global settings instance
settings = Settings()
