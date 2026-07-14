#!/usr/bin/env python3
"""Initialize database schemas"""

import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import postgres, clickhouse
from app.config import settings
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    """Initialize all databases"""
    logger.info("Starting database initialization...")

    try:
        # Initialize PostgreSQL
        logger.info("Initializing PostgreSQL...")
        await postgres.init_pool()
        await postgres.execute_schema()
        logger.info("✅ PostgreSQL schema created")

        # Initialize ClickHouse
        logger.info("Initializing ClickHouse...")
        clickhouse.init_client()
        clickhouse.execute_schema()
        logger.info("✅ ClickHouse schema created")

        logger.info("\n🎉 All databases initialized successfully!")
        logger.info(
            f"PostgreSQL: {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
        )
        logger.info(
            f"ClickHouse: {settings.CLICKHOUSE_HOST}:{settings.CLICKHOUSE_PORT}/{settings.CLICKHOUSE_DB}"
        )

    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        sys.exit(1)
    finally:
        await postgres.close_pool()
        clickhouse.close_client()


if __name__ == "__main__":
    asyncio.run(main())
