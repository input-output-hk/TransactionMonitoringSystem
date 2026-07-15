# Contributing

Thanks for your interest in the Cardano Transaction Monitoring System (TMS). This guide covers how to set the project up, the rules that every change must follow, and how to get a change merged.

## Code of conduct

Be respectful and constructive. Assume good faith, keep discussion technical, and help newcomers. Harassment or abuse of any kind is not tolerated.

## Project layout

| Path | What it is |
|---|---|
| `backend/` | FastAPI application: ingestion via Ogmios, storage in ClickHouse and PostgreSQL, the nine attack-class scorers (`backend/app/analysis/scorers/`), and the REST and WebSocket API. |
| `frontend/` | React and TypeScript operator dashboard (Vite, pnpm). |
| `services/clustering/` | Optional first-party sidecar that adds the `contract_anomaly` class via unsupervised per-contract profiling. |
| `config/detection.yaml` | The tunable thresholds, weights, and windows for the scorers. |
| `docs/` | Architecture, detection spec, data flow, and license docs. See [docs/README.md](docs/README.md). |

## Development setup

```bash
# Backend (uv creates .venv from uv.lock, dev tools included)
uv sync

# Databases (Postgres + ClickHouse; add `--profile mail` for the Mailpit dev SMTP sink)
cp .env.example .env
docker-compose up -d

# Frontend (built into the backend image for production; run it directly for UI work)
cd frontend
pnpm install
pnpm dev
```

Dependencies are declared in the root `pyproject.toml`. After editing them run
`uv lock`, then regenerate the hashed lock the Docker image installs:
`uv export --no-dev --no-emit-project -o requirements.lock`.

The clustering sidecar uses its own environment (it is not installed into the root venv):

```bash
cd services/clustering/backend
uv sync --extra dev
```

See the root [README.md](README.md) and [RUNBOOK.md](RUNBOOK.md) for the full setup and operations guide, and [services/clustering/README.md](services/clustering/README.md) for the sidecar.

## The rules every change must follow

These are not style preferences. They reflect what this system is for.

### 1. Recall first

This is a transaction monitoring system. The cost of the two error types is not symmetric, so the priority order is fixed:

- **Never miss a real attack.** A missed attack (false negative) is the worst possible outcome and takes precedence over every other concern.
- **Minimize false positives**, but only after recall is secured.
- **When in doubt, a false positive is better than a missed real attack.** If a tuning decision trades recall for precision and the call is not clear-cut, keep the recall.

Every gate, threshold, and weight is calibrated against this order. Tighten precision only when you can show the real-attack cases still fire.

### 2. Verify recall after every detection change

After any change to detection parameters (`config/detection.yaml`, scorer config) or scorer code, prove there is no recall regression before opening the PR:

- Run the scorer suite: `cd backend && uv run pytest tests/analysis/`. The positive, attack-must-fire cases (for example `*_passes_*`, `*_high_score`, `*_composite_reason`) must all still pass. A precision-tuning change may only remove false positives, never silence a true detection.
- A change that narrows a gate or raises a threshold must come with a test proving the real-attack case it is meant to preserve still scores above its band. If no such test exists yet, add it.
- If a change cannot be shown to preserve recall, do not ship it. Surface the trade-off in the PR instead.

### 3. No magic numbers

A magic number is a numeric literal whose meaning is not obvious from context.

- Tunable thresholds, weights, anchors, ratios, byte sizes, and time windows live in `config/detection.yaml` (or the subsystem's config file) and load through the validated config loader. Tests reference the same config, not a duplicated value.
- Derived constants that compose named values (for example `BAND_HIGH_THRESHOLD - 1`) must be a named module-level constant with a one-line comment explaining the offset.
- Sentinel values (for example `-1` for "no finding") are acceptable inline only when they match a documented schema convention, stated in a comment at the call site.
- Permitted bare literals: `0`, `1`, `-1`, `True`, `False`, `None`, list indices, and mathematical identities (`* 2`, `/ 2`, `+ 1`).

Comments explain why a constant has its value (the threat model, the protocol limit, the observed distribution), not what it does.

### 4. Documentation style

- Do not use `---` horizontal rules as section dividers.
- Do not use em dashes. Use a colon, comma, semicolon, or rephrase the sentence.
- Use `:` instead of a dash in headings (for example `## Chain Sync: How a Block Travels`).

## Running the tests

| Suite | Command | Notes |
|---|---|---|
| Backend | `cd backend && uv run pytest tests/` | Hermetic: no live ClickHouse, node, or network needed. |
| Backend recall gate | `cd backend && uv run pytest tests/analysis/` | The attack-must-fire cases. Must stay green on every detection change. |
| Clustering sidecar | `cd services/clustering/backend && uv run pytest -q` | Needs its own env (`uv sync --extra dev`). |
| Frontend | `cd frontend && pnpm lint && pnpm build` | Lint, then the type-checked build. |
| Lint + types (backend) | `uv run ruff check backend && uv run mypy backend/app` | Also `ruff format --check backend`. CI runs all three. |
| Lint + types (sidecar) | `cd services/clustering/backend && uv run ruff check . && uv run mypy app` | The sidecar runs `disallow_untyped_defs` globally. |

mypy runs at a lenient baseline (untyped defs allowed) with a growing strict
cohort: modules listed in the root `pyproject.toml` `[[tool.mypy.overrides]]`
block hold to `disallow_untyped_defs`. When you fully annotate a module, add it
to that list (confirm with `uv run mypy <module> --disallow-untyped-defs`); the
aim is to grow the cohort until strict can become the global default.

## Commits and pull requests

- Use Conventional Commit messages with a scope, matching the existing history: `fix(clustering): ...`, `feat(frontend): ...`, `chore(maintenance): ...`.
- Keep pull requests focused. Describe what changed and why, and call out any recall or precision trade-off.
- Sign off your commits to certify you have the right to submit the work under the project license, per the [Developer Certificate of Origin](https://developercertificate.org):

  ```bash
  git commit -s -m "fix(scorers): ..."
  ```

  This appends a `Signed-off-by: Your Name <you@example.com>` line.

By contributing, you agree that your contributions are licensed under the project's [Apache License 2.0](LICENSE).
