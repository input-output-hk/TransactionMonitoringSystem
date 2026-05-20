FROM python:3.12-slim

# Run as a non-root user to limit blast radius of a container escape.
RUN groupadd --system --gid 1001 appgroup \
 && useradd --system --uid 1001 --gid 1001 --no-create-home appuser

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy config files (e.g., detection.yaml)
COPY --chown=appuser:appgroup ../config/ ./config/

# Copy application code with correct ownership
COPY --chown=appuser:appgroup backend/ ./backend/
WORKDIR /app/backend

# Create /data/raw directory with correct ownership for raw_store
RUN mkdir -p /data/raw && chown -R appuser:appgroup /data/raw

USER appuser

# Expose port
EXPOSE 8000

# Run the application (reload disabled by default; set UVICORN_RELOAD=true for dev)
CMD ["python", "run.py"]
