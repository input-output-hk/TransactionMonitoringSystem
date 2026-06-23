# Third-Party Licenses

All production and development dependencies used in this project carry permissive
open-source licenses (MIT, BSD-3-Clause, Apache-2.0, or equivalent).  None of the
direct dependencies are GPL, LGPL, or AGPL.

The one exception worth noting is **Ogmios**, which is licensed under MPL-2.0.
Ogmios is consumed as an **external network service** (WebSocket connection); it is
not compiled into or linked against this codebase.  The MPL-2.0 file-level copyleft
therefore does not extend to this project's source code.

## Python Libraries (`requirements.txt`)

| Package | Pinned version | License | SPDX identifier | Category |
|---|---|---|---|---|
| fastapi | 0.133.0 | MIT | `MIT` | Web framework |
| uvicorn\[standard\] | 0.41.0 | MIT | `MIT` | ASGI server |
| pydantic | 2.12.5 | MIT | `MIT` | Data validation |
| pydantic-settings | 2.13.1 | MIT | `MIT` | Settings management |
| websockets | ≥16.0, <17.0 | BSD 3-Clause | `BSD-3-Clause` | WebSocket client (Ogmios) |
| clickhouse-driver | 0.2.10 | MIT | `MIT` | ClickHouse DB driver |
| asyncpg | 0.31.0 | Apache 2.0 | `Apache-2.0` | PostgreSQL async driver |
| httpx | 0.28.1 | BSD 3-Clause | `BSD-3-Clause` | HTTP client (clustering sidecar proxy) |
| python-dotenv | 1.2.1 | BSD 3-Clause | `BSD-3-Clause` | `.env` file loading |
| pytest | 9.0.2 | MIT | `MIT` | Test runner |
| pytest-asyncio | 1.3.0 | Apache 2.0 | `Apache-2.0` | Async test support |
| pytest-cov | 7.0.0 | MIT | `MIT` | Coverage reporting |
| black | 26.1.0 | MIT | `MIT` | Code formatter |
| ruff | 0.15.2 | MIT | `MIT` | Linter |
| mypy | 1.19.1 | MIT | `MIT` | Static type checker |

`uvicorn[standard]` pulls in these transitive extras:

| Transitive extra | License | Notes |
|---|---|---|
| uvloop | MIT | Fast event loop (Linux/macOS) |
| httptools | MIT | Fast HTTP parser |

## Frontend Libraries (`frontend/package.json`)

The host SPA is built with React + Vite. The clustering UI surfaces (the Validators
views) add a small number of libraries on top of the existing Radix/Recharts/Cytoscape
stack; the ones introduced for that work are recorded here. All are permissive (MIT).

| Package | License | SPDX identifier | Used for |
|---|---|---|---|
| @radix-ui/react-tabs | MIT | `MIT` | Tab strip on the validator detail view |
| plotly.js-dist-min | MIT | `MIT` | 2-D/3-D feature-space projection scatter (code-split) |
| react-plotly.js | MIT | `MIT` | React wrapper around the Plotly bundle |

## Infrastructure: Docker Images

| Image | Version | License | Notes |
|---|---|---|---|
| `python` | 3.12-slim | PSF-2.0 (Python Software Foundation) | Permissive; equivalent to MIT for distribution purposes |
| `postgres` | 18-alpine | PostgreSQL License | OSI-approved permissive license (BSD-style, 2 clauses) |
| `clickhouse/clickhouse-server` | 26.1.3 | Apache 2.0 | |

## External Services

| Service | Version | License | Integration method | Copyleft impact |
|---|---|---|---|---|
| Ogmios | v6 | MPL-2.0 (Mozilla Public License 2.0) | WebSocket network connection, not linked or compiled in | **None.** MPL-2.0 is file-level copyleft; it does not extend across a network boundary. This project's source files are unaffected. |

## Bundled data

The clustering module ships a point-in-time snapshot of the StricaHQ
[cardano-contracts-registry](https://github.com/StricaHQ/cardano-contracts-registry)
under `services/clustering/backend/app/registry/data/`, used to label known
script addresses and minting policies. It is Apache-2.0 (same as this project);
the upstream license is retained verbatim alongside the data at
`services/clustering/backend/app/registry/data/LICENSE`.

| Bundled asset | Source | License | Integration method |
|---|---|---|---|
| Cardano contracts registry snapshot | StricaHQ/cardano-contracts-registry | Apache-2.0 | Data files only (not code); refreshed via the registry sync helper |

## License compatibility summary

| License | Count | Permissive? | Conditions |
|---|---|---|---|
| MIT | 11 | Yes | Attribution in distributed copies only |
| Apache 2.0 | 4 | Yes | Attribution + patent grant; compatible with MIT/BSD. Includes the bundled StricaHQ registry snapshot (data, see "Bundled data"). |
| BSD 3-Clause | 3 | Yes | Attribution; no endorsement clause |
| PSF-2.0 | 1 | Yes | Attribution |
| PostgreSQL License | 1 | Yes | Attribution (BSD-style, 2 clauses) |
| MPL-2.0 | 1 (external service) | File-level copyleft | Does not apply; consumed as a service over the network |

All direct code dependencies are **compatible with Apache 2.0 and with each other**.
No GPL, LGPL, AGPL, EUPL, or other strong-copyleft license is present in the
dependency tree.

*Last updated: 2026-06-23. Verify against PyPI metadata when upgrading pinned versions.*
