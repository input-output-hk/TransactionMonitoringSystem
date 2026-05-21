# Stage 1: build the React/Vite frontend.
# VITE_TMS_API_KEY is baked into the bundle, so callers in the browser
# get the right auth header without a separate sign-in flow.
FROM node:22-alpine AS frontend-build
WORKDIR /frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
ARG VITE_TMS_API_KEY
ARG VITE_NETWORK=preprod
ENV VITE_TMS_API_KEY=${VITE_TMS_API_KEY}
ENV VITE_NETWORK=${VITE_NETWORK}
RUN pnpm build

# Stage 2: python app, with the built SPA copied in for FastAPI to serve.
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

# Built frontend assets — served by FastAPI as StaticFiles
COPY --from=frontend-build --chown=appuser:appgroup /frontend/dist /app/frontend-dist

WORKDIR /app/backend

# Create /data/raw directory with correct ownership for raw_store
RUN mkdir -p /data/raw && chown -R appuser:appgroup /data/raw

USER appuser

# Expose port
EXPOSE 8000

# Run the application (reload disabled by default; set UVICORN_RELOAD=true for dev)
CMD ["python", "run.py"]
