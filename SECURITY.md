# Security Policy

The Cardano Transaction Monitoring System (TMS) is a defensive tool: it detects on-chain attacks and anomalies. We take the security of the project itself seriously and welcome reports from the community.

## Reporting a vulnerability

Please do not open a public issue for a security vulnerability.

Report it privately using GitHub's private vulnerability reporting: on the repository's **Security** tab choose **Report a vulnerability**. This opens a private advisory visible only to you and the maintainers.

When reporting, please include:

- A description of the issue and the component affected (backend API, auth, ingestion, clustering sidecar, or frontend).
- Steps to reproduce, or a proof of concept.
- The impact you believe it has, and any suggested remediation.
- The commit or version you tested against.

## What to expect

- We aim to acknowledge a report within 3 business days.
- We will confirm the issue, assess its severity, and keep you updated on remediation progress.
- We will credit reporters who wish to be named once a fix is released. Coordinated disclosure is appreciated: please give us a reasonable window to ship a fix before any public write-up.

## Scope

In scope: the code in this repository (backend, frontend, clustering sidecar, and deployment manifests).

Out of scope: third-party services this project connects to (for example a Cardano node, Ogmios, or your SMTP provider), and issues that require an attacker to already hold host or database write access in a single-box demo deployment.

## Deploying safely

This repository ships secure by default. A few operational notes for anyone running it beyond a local machine:

- **Authentication is fail-closed.** The application refuses to start with an empty `API_KEYS`, an empty ClickHouse password, the default Postgres password, or a wildcard CORS origin, unless `TMS_ALLOW_DEV_MODE=1` is set explicitly. Dev mode disables authentication entirely and is for local use only: never expose a dev-mode instance beyond `localhost`.
- **Bind to trusted networks.** The Compose services bind to `127.0.0.1` by default. Put the API behind a reverse proxy with TLS before exposing it, and narrow `TRUSTED_PROXY_CIDRS` to your actual proxy address.
- **Clustering model store.** When the ClickHouse instance is shared, set `MODEL_SIGNING_KEYS` so the clustering sidecar verifies model blobs before loading them.
- **Local mail capture.** The bundled Mailpit service (opt-in via `--profile mail`; not started by the default deployment) captures magic-link emails for local development. It is unauthenticated and must not be exposed publicly.

See [RUNBOOK.md](RUNBOOK.md) for the full configuration reference.
