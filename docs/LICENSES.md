# Third-Party Licenses

This project's dependencies are, with a small and documented set of exceptions, licensed under Apache-2.0 or a more permissive license (MIT, BSD-2-Clause, BSD-3-Clause, ISC, PSF-2.0, 0BSD, Zlib, Unlicense, or CC0). There is no strong copyleft anywhere in the dependency tree: no GPL, LGPL, AGPL, SSPL, BUSL, EPL, or CDDL.

The exceptions are a handful of transitive dependencies under weak (file-level) copyleft or an attribution-only data license. None are modified by this project or linked into its source in a way that imposes obligations on this project's own code. They are itemised in [Weak-copyleft and attribution dependencies](#weak-copyleft-and-attribution-dependencies).

## How this was verified

Licenses were resolved from installed package metadata, not inferred from names:

- Python (backend and clustering sidecar): read from each installed distribution's metadata, preferring the PEP 639 `License-Expression` (SPDX), then `License ::` classifiers, then the `License` field, across the fully installed runtime and dev trees. Ambiguous or empty cases were confirmed against the bundled `LICENSE` file.
- Frontend: resolved with `pnpm licenses list` over the installed `node_modules`, with the few `SEE LICENSE IN` and no-field packages confirmed from their bundled license text.

Coverage: the backend `requirements.txt` tree (56 installed distributions), the frontend `package.json` tree (495 packages), and the clustering sidecar's runtime plus `[dev]` tree (55 third-party packages). The sidecar's optional `[notebook]` extra (matplotlib, jupyter) is not shipped in any image and is not covered here: matplotlib and the jupyter metapackage are themselves permissive (BSD or PSF style), but their large transitive trees should be reviewed before that extra is ever shipped.

## Weak-copyleft and attribution dependencies

These are the only dependencies not under Apache-2.0 or a more permissive license. All are weak (file-level) copyleft or attribution-only, none are strong copyleft, and none affect this project's own source.

| Dependency | License | Subsystem | Used at | How it is pulled in | Obligation impact |
|---|---|---|---|---|---|
| `certifi` | MPL-2.0 | backend, clustering | runtime | Transitive via httpx, requests, urllib3, clickhouse-connect. A bundle of CA certificates. | MPL-2.0 copyleft is per file and triggers only on modifying certifi's own files. It is used unmodified, so no obligation extends to this project. |
| `pathspec` | MPL-2.0 | backend, clustering | dev only | Transitive via the `black` / `mypy` / `ruff` toolchain. Not present in any runtime image. | Same per-file copyleft, used unmodified, and not redistributed in the product. |
| `lightningcss` (and its platform binary) | MPL-2.0 | frontend | build only | Transitive via Tailwind CSS v4 / Vite. A Rust CSS transformer run during the build. | Per-file copyleft, used unmodified. Runs at build time and is not part of the shipped JS bundle. |
| `caniuse-lite` | CC-BY-4.0 | frontend | build only | Transitive via `browserslist` / autoprefixer. A browser-support dataset, not code. | CC-BY-4.0 requires attribution only. It is data consulted at build time, not redistributed as part of the app. |

The external **Ogmios** service is MPL-2.0 as well; see [External services](#external-services).

If a strict "100 percent Apache-2.0 or more permissive" bar is ever required, note that these are not cheaply removable: `pathspec` is a dependency of both `black` and `mypy`, so it remains as long as static type checking is kept, and `certifi` would have to be replaced by pointing the Python HTTP clients at the system trust store. `lightningcss` and `caniuse-lite` are intrinsic to the Tailwind v4 and browserslist build chains and are not practically removable. None of this is necessary for an Apache-2.0 release: MPL-2.0 and CC-BY-4.0 are OSI and FSF approved and are compatible with redistributing this project under Apache-2.0.

## Backend Python libraries (`requirements.txt`)

Direct dependencies, with versions as pinned in `requirements.txt`:

| Package | Version | License | SPDX identifier | Category |
|---|---|---|---|---|
| fastapi | 0.138.0 | MIT | `MIT` | Web framework |
| uvicorn\[standard\] | 0.49.0 | BSD 3-Clause | `BSD-3-Clause` | ASGI server |
| pydantic | 2.13.4 | MIT | `MIT` | Data validation |
| pydantic-settings | 2.14.2 | MIT | `MIT` | Settings management |
| websockets | ≥16.0, <17.0 | BSD 3-Clause | `BSD-3-Clause` | WebSocket client (Ogmios) |
| clickhouse-driver | 0.2.10 | MIT | `MIT` | ClickHouse driver |
| asyncpg | 0.31.0 | Apache 2.0 | `Apache-2.0` | PostgreSQL async driver |
| httpx | 0.28.1 | BSD 3-Clause | `BSD-3-Clause` | HTTP client (clustering sidecar proxy) |
| aiosmtplib | 3.0.2 | MIT | `MIT` | Async SMTP (magic-link mail) |
| email-validator | 2.3.0 | Unlicense | `Unlicense` | Email validation (Pydantic `EmailStr`) |
| python-dotenv | 1.2.2 | BSD 3-Clause | `BSD-3-Clause` | `.env` file loading |
| PyYAML | 6.0.3 | MIT | `MIT` | YAML config loading |
| cbor2 | 6.1.2 | MIT | `MIT` | CBOR decoding (datum analysis) |
| rapidfuzz | 3.14.5 | MIT | `MIT` | Fuzzy string matching (phishing) |
| tldextract | 5.3.1 | BSD 3-Clause | `BSD-3-Clause` | Domain parsing (phishing) |
| pytest | 9.1.1 | MIT | `MIT` | Test runner |
| pytest-asyncio | 1.4.0 | Apache 2.0 | `Apache-2.0` | Async test support |
| pytest-cov | 7.1.0 | MIT | `MIT` | Coverage reporting |
| black | 26.5.1 | MIT | `MIT` | Code formatter |
| ruff | 0.15.18 | MIT | `MIT` | Linter |
| mypy | 2.1.0 | MIT | `MIT` | Static type checker |

The full installed tree (56 distributions, including transitive dependencies such as `starlette` BSD-3-Clause, `anyio` MIT, `idna` BSD-3-Clause, `click` BSD-3-Clause, `requests` Apache-2.0, `dnspython` ISC, `typing_extensions` PSF-2.0, and the `uvicorn[standard]` extras `uvloop` and `httptools`, both MIT) was audited. Every package is Apache-2.0 or more permissive except `certifi` and `pathspec` (MPL-2.0), documented above.

## Clustering sidecar Python libraries (`services/clustering/backend`)

Direct dependencies from `pyproject.toml`; exact versions are pinned in `uv.lock`.

Runtime:

| Package | License | SPDX identifier | Category |
|---|---|---|---|
| clickhouse-connect | Apache 2.0 | `Apache-2.0` | ClickHouse driver |
| scikit-learn | BSD 3-Clause | `BSD-3-Clause` | Clustering (DBSCAN) |
| numpy | BSD 3-Clause | `BSD-3-Clause` | Numerics (bundles permissive 0BSD/MIT/Zlib/CC0 components) |
| pandas | BSD 3-Clause | `BSD-3-Clause` | Dataframes |
| scipy | BSD 3-Clause | `BSD-3-Clause` | Scientific computing |
| kneed | BSD 3-Clause | `BSD-3-Clause` | Knee/elbow detection |
| networkx | BSD 3-Clause | `BSD-3-Clause` | Graph analysis |
| fastapi | MIT | `MIT` | Web framework |
| uvicorn\[standard\] | BSD 3-Clause | `BSD-3-Clause` | ASGI server |
| pydantic | MIT | `MIT` | Data validation |
| pydantic-settings | MIT | `MIT` | Settings management |
| typer | MIT | `MIT` | CLI framework |

Dev (`[dev]` extra):

| Package | License | SPDX identifier |
|---|---|---|
| httpx | BSD 3-Clause | `BSD-3-Clause` |
| pytest | MIT | `MIT` |
| pytest-asyncio | Apache 2.0 | `Apache-2.0` |
| ruff | MIT | `MIT` |
| mypy | MIT | `MIT` |

The full installed runtime plus dev tree (55 third-party packages, including transitive `joblib`, `threadpoolctl`, `scipy`, `zstandard`, `lz4` all BSD-3-Clause, `rich` and `six` MIT, `shellingham` ISC, `python-dateutil` dual Apache-2.0/BSD-3-Clause) was audited. Every package is Apache-2.0 or more permissive except `certifi` and `pathspec` (MPL-2.0, shared with the backend and documented above).

## Frontend libraries (`frontend/package.json`)

Runtime dependencies that ship in the dashboard bundle (all permissive):

| Package | Version | License | SPDX identifier |
|---|---|---|---|
| react / react-dom | 19.2.6 | MIT | `MIT` |
| react-router-dom | 7.15.0 | MIT | `MIT` |
| @tanstack/react-query | 5.100.10 | MIT | `MIT` |
| zustand | 5.0.13 | MIT | `MIT` |
| @radix-ui/react-* (avatar, dialog, dropdown-menu, select, separator, slot, tabs, tooltip) | 1.x–2.x | MIT | `MIT` |
| recharts | 3.8.1 | MIT | `MIT` |
| cytoscape | 3.30.2 | MIT | `MIT` |
| cytoscape-fcose | 2.2.0 | MIT | `MIT` |
| plotly.js-dist-min | 3.6.0 | MIT | `MIT` |
| react-plotly.js | 4.0.0 | MIT | `MIT` |
| papaparse | 5.5.3 | MIT | `MIT` |
| sonner | 2.0.7 | MIT | `MIT` |
| clsx | 2.1.1 | MIT | `MIT` |
| tailwind-merge | 3.6.0 | MIT | `MIT` |
| class-variance-authority | 0.7.1 | Apache 2.0 | `Apache-2.0` |
| lucide-react | 1.14.0 | ISC | `ISC` |

Build and dev dependencies (Vite, TypeScript, ESLint, Prettier, Tailwind toolchain and types) are all MIT, except `typescript` (Apache-2.0).

Full-tree breakdown from `pnpm licenses list` (495 packages):

| License | Count | Permissive? |
|---|---|---|
| MIT | 379 | Yes |
| ISC | 50 | Yes |
| BSD-3-Clause | 26 | Yes |
| Apache-2.0 | 16 | Yes |
| BSD-2-Clause | 10 | Yes |
| BlueOak-1.0.0 | 3 | Yes |
| MPL-2.0 (`lightningcss` + platform binary) | 2 | Weak copyleft, build only (see above) |
| MIT AND ISC (`victory-vendor`) | 1 | Yes |
| Unlicense | 1 | Yes |
| Zlib (`gl-mat4`) | 1 | Yes |
| 0BSD | 1 | Yes |
| CC-BY-4.0 (`caniuse-lite`) | 1 | Attribution data, build only (see above) |

Five packages report a non-SPDX or missing `license` field and were resolved from their bundled license text: `mapbox-gl` and `@plotly/mapbox-gl` are BSD-3-Clause, `stack-trace` is MIT, `@mapbox/jsonlint-lines-primitives` is MIT (inherited from upstream `zaach/jsonlint`; no explicit field in the fork). Everything is Apache-2.0 or more permissive except the `lightningcss` (MPL-2.0) and `caniuse-lite` (CC-BY-4.0) build-time dependencies documented above.

## Infrastructure: Docker images

These run as separate containers and are not linked or compiled into the product.

| Image | Version | License | Notes |
|---|---|---|---|
| `python` | 3.12-slim (backend), 3.13-slim (clustering) | PSF-2.0 | Permissive; equivalent to MIT for distribution purposes |
| `node` | 22-alpine (frontend build stage) | MIT | Node.js core is MIT; bundled OpenSSL etc. are permissive |
| `postgres` | 18-alpine | PostgreSQL License | OSI-approved permissive (BSD-style, 2 clauses) |
| `clickhouse/clickhouse-server` | 26.1.3 | Apache 2.0 | |
| `ghcr.io/astral-sh/uv` | 0.5.31 | Apache-2.0 OR MIT | Build-stage tool for the clustering image |
| `axllent/mailpit` | (dev only) | MIT | Local magic-link mail capture; not for production |
| `ghcr.io/intersectmbo/cardano-node` | 11.0.1 | Apache 2.0 | `ingestion` profile only |
| `cardanosolutions/ogmios` | v6.14.0 | MPL-2.0 | `ingestion` profile only; see External services |

## External services

| Service | Version | License | Integration method | Copyleft impact |
|---|---|---|---|---|
| Ogmios | v6 | MPL-2.0 (Mozilla Public License 2.0) | WebSocket network connection, not linked or compiled in | None. MPL-2.0 is file-level copyleft and does not extend across a network boundary. This project's source files are unaffected. |

## Bundled data

The clustering module ships a point-in-time snapshot of the StricaHQ [cardano-contracts-registry](https://github.com/StricaHQ/cardano-contracts-registry) under `services/clustering/backend/app/registry/data/`, used to label known script addresses and minting policies. It is Apache-2.0 (the same license as this project); the upstream license is retained verbatim alongside the data at `services/clustering/backend/app/registry/data/LICENSE`.

| Bundled asset | Source | License | Integration method |
|---|---|---|---|
| Cardano contracts registry snapshot | StricaHQ/cardano-contracts-registry | Apache-2.0 | Data files only (not code); refreshed via the registry sync helper |

## License compatibility summary

| License | Where | Permissive vs Apache-2.0 | Conditions |
|---|---|---|---|
| MIT | all subsystems (majority) | More permissive | Attribution in distributed copies |
| BSD-2-Clause / BSD-3-Clause | all subsystems | More permissive | Attribution; (3-clause) no endorsement |
| ISC | frontend, sidecar | More permissive | Attribution |
| 0BSD / Unlicense / CC0-1.0 | transitive | More permissive | None (public-domain-equivalent) |
| Zlib / BlueOak-1.0.0 | frontend (transitive) | More permissive | Attribution |
| PSF-2.0 | Python interpreter, typing_extensions | More permissive | Attribution |
| Apache-2.0 | this project + several deps | Baseline | Attribution + patent grant |
| MPL-2.0 | certifi, pathspec, lightningcss, Ogmios | Weak (file-level) copyleft | Share modifications to the covered files only; not triggered here |
| CC-BY-4.0 | caniuse-lite (build data) | Attribution-only data license | Attribution |

No GPL, LGPL, AGPL, SSPL, BUSL, EPL, CDDL, or other strong-copyleft license is present in any dependency tree. The four weak-copyleft and attribution dependencies are documented above and impose no obligation on this project's source.

*Last updated: 2026-06-26. Resolved from installed package metadata (Python, via PEP 639 / classifiers) and `pnpm licenses list` (frontend). Re-verify when upgrading pinned versions.*
