# TMS Operator Dashboard (frontend)

This is the operator dashboard for the Cardano Transaction Monitoring System: a single-page app built with React, TypeScript, and Vite. In production it is compiled to static assets and served by the FastAPI backend from the same origin as the API, so there is no separate frontend deployment. In development it runs on the Vite dev server and talks to a host backend through a dev proxy, giving you hot-module reload against a real API.

## Prerequisites

- Node 22 (the production image builds on `node:22-alpine`).
- pnpm. This package uses pnpm, not npm; the committed lockfile is `pnpm-lock.yaml` and the Docker build installs with `--frozen-lockfile`. Enable it with `corepack enable` if you do not already have it.

Install dependencies with `pnpm install`.

## Scripts

| Script | Command | What it does |
|---|---|---|
| `dev` | `vite` | Start the dev server with HMR (proxies the API to a host backend, see below). |
| `build` | `tsc -b && vite build` | Type-check the project references, then produce the production bundle in `dist/`. |
| `lint` | `eslint .` | Lint the whole project. |
| `test` | `vitest run` | Run the unit test suite once. |
| `test:watch` | `vitest` | Run the tests in watch mode. |
| `preview` | `vite preview` | Serve the built `dist/` locally to sanity-check a production build. |
| `format` | `prettier --write "src/**/*.{ts,tsx,css,json}"` | Format the source tree. |
| `format:check` | `prettier --check "src/**/*.{ts,tsx,css,json}"` | Verify formatting without writing. |

CI runs `lint`, `test`, and `build` on every change; keep all three green.

## Testing

Unit tests run on Vitest with the `jsdom` environment and Testing Library (`@testing-library/react` plus the `@testing-library/jest-dom` matchers). Test files are colocated with the code they cover and match `src/**/*.test.{ts,tsx}` (configured in `vite.config.ts`). Use `pnpm test` for a one-shot run or `pnpm test:watch` while iterating.

## Dev proxy: running against a host backend

`vite.config.ts` proxies `/api` and `/health` to `http://localhost:8000`, so the SPA can call the API same-origin while the dev server owns the page. This means `pnpm dev` expects a backend already running on port 8000. Start one first (see the repo README section "Local development (host Python)"), then run `pnpm dev` and open the printed dev URL; requests to `/api/*` and `/health` are forwarded to that backend.

## Environment variables

Build-time variables are read from Vite's `import.meta.env` and must be prefixed with `VITE_`.

| Variable | Values | Default | Purpose |
|---|---|---|---|
| `VITE_NETWORK` | `mainnet` \| `preprod` \| `preview` | `preprod` | The Cardano network the dashboard targets. It is sent as the `network` query param on `/api/v1/analysis/*` and `/api/v1/archive/*`, and the default matches the backend's default. |
| `VITE_USE_MOCK_ARCHIVE_API` | `"true"` to enable | unset (real backend) | Dev-only opt-in to a `localStorage`-backed mock of the archive API, for working offline when the backend is not reachable. It is honored only when `import.meta.env.DEV` is true; production builds always use the real client, so a stray flag cannot switch a deployment to `localStorage`. |

Note: there is no `VITE_TMS_API_KEY`. It was removed because a build-time API key gets baked into the public JS bundle, where anyone can read it from DevTools and call the API directly. Browser authentication is now the magic-link session cookie (`tms_session`, set by the backend), with a CSRF double-submit cookie on mutating requests; the SPA never carries an API key. (The backend still accepts an `API_KEYS` env for server-to-server callers such as the CLI, but the dashboard does not send one.) If you find `VITE_TMS_API_KEY` referenced anywhere, it is stale and should be removed.

## Production build: how the SPA is embedded in the backend image

The dashboard ships inside the backend Docker image; there is no standalone frontend container. `backend/Dockerfile` does this in two stages:

1. Stage 1 (`node:22-alpine`, named `frontend-build`) enables corepack, runs `pnpm install --frozen-lockfile`, copies the frontend source, and runs `pnpm build`. It takes the target network from a `VITE_NETWORK` build argument (default `preprod`), exported into the build environment so the bundle is built for the right network.
2. Stage 2 (the Python app image) copies the compiled `frontend/dist` from stage 1 into `/app/frontend-dist`, where FastAPI serves it as static files alongside the API on the same origin.

`docker-compose.yml` wires the build argument through as `VITE_NETWORK: ${VITE_NETWORK:-preprod}`, so setting `VITE_NETWORK` in your environment (or `.env`) before `docker compose build` selects the network the shipped dashboard targets.
