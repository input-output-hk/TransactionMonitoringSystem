# Stage 1: build the React/Vite frontend.
# Browser-side auth is the magic-link session cookie set by /api/auth/verify
# — no API key is baked into the bundle. The backend's API_KEYS env stays
# for server-to-server callers (CLI, integrations) but the SPA never sees it.
FROM node:22-alpine AS frontend-build
WORKDIR /frontend
RUN corepack enable
# pnpm-workspace.yaml carries the allowBuilds policy (e.g. es5-ext: false); pnpm
# needs it present or `install` fails the ignored-build-scripts gate.
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
ARG VITE_NETWORK=preprod
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

# Copy config files (e.g., detection.yaml). The compose build context is the
# repo root (see docker-compose.yml: build.context = .), so config/ is reached
# directly; `../config/` pointed outside the context and failed the build.
COPY --chown=appuser:appgroup config/ ./config/

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
