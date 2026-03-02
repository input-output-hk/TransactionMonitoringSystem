#!/usr/bin/env python3
"""Entry point for running the Cardano Transaction Monitoring System"""

import os
import uvicorn
from app.config import settings

if __name__ == "__main__":
    # File-watching reload is a development convenience only; never enable it in
    # a Docker container or production host (spawns a watchdog subprocess,
    # adds latency, and can cause unexpected restarts).
    # Enable locally with: UVICORN_RELOAD=true python run.py
    reload = os.getenv("UVICORN_RELOAD", "false").lower() == "true"

    uvicorn.run(
        "app.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=reload,
        log_level=settings.LOG_LEVEL.lower()
    )
