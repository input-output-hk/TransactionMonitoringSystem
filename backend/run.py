#!/usr/bin/env python3
"""Entry point for running the Cardano Transaction Monitoring System"""

import uvicorn
from app.config import settings

if __name__ == "__main__":
    # File-watching reload is a development convenience only; never enable it in
    # a Docker container or production host (spawns a watchdog subprocess,
    # adds latency, and can cause unexpected restarts).
    # Enable locally via .env (UVICORN_RELOAD=true) or the shell.
    uvicorn.run(
        "app.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.UVICORN_RELOAD,
        log_level=settings.LOG_LEVEL.lower()
    )
