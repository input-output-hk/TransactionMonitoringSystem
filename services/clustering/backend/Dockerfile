# syntax=docker/dockerfile:1

# ---- builder: install the project + runtime deps from pyproject -----------------
FROM python:3.13-slim AS builder

# Pinned uv (exact patch, not :latest/:0.5) for reproducible builds.
COPY --from=ghcr.io/astral-sh/uv:0.5.31 /uv /bin/uv

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# pyproject.toml is the single source of truth — runtime deps are no longer
# duplicated here (they used to drift).
COPY pyproject.toml ./
COPY app ./app
# Algorithm tunables: app/tunables.py resolves config/clustering.yaml by walking
# up from the app package, so the config must sit next to it in every stage that
# runs the code (the dev stage inherits this copy).
COPY config ./config
RUN uv pip install --system .

# ---- dev: builder + dev tooling (pytest/ruff/mypy). Used by docker-compose for
#      the local loop, where ./backend is bind-mounted so code + tests are live.
#      Not the default target, so a bare `docker build` produces the slim image. --
FROM builder AS dev
RUN uv pip install --system ".[dev]"
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---- runtime: slim, non-root, no dev deps / tests. Default (last) stage, so
#      `docker build ./backend` yields the production image. ----------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Bring over the installed interpreter packages + console scripts only.
COPY --from=builder /usr/local /usr/local

WORKDIR /app
COPY app ./app
# The tunables loader walks up from /app/app to find /app/config/clustering.yaml.
COPY config ./config

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
