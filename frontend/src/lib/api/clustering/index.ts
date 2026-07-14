/**
 * Client for the clustering module, reached through the `/api/v1/clustering`
 * reverse-proxy (session-authed, same-origin). Powers the Validators surfaces:
 * the watched-contract registry, cluster summaries, anomaly tables, and the
 * cluster graph.
 *
 * The clustering module auto-feeds and auto-fits each watched contract as the
 * chain is ingested, producing the canonical `system` run that drives scoring.
 * On top of that read path this client also exposes manual, secondary controls
 * for analysts: evaluate DBSCAN parameters, run a custom clustering, detect
 * anomalies, and label clusters/transactions. Manual `cluster`/`anomaly` passes
 * create `custom` runs that do NOT supersede the system run for scoring.
 *
 * This module is split across sibling files: `types` (shapes), `validation`
 * (runtime guards, internal), `transport` (the fetch wrappers, internal), and
 * `hooks` (the React Query surface). This barrel re-exports the public API
 * (types + hooks) so consumers keep importing from `@/lib/api/clustering`.
 */
export * from "./types";
export * from "./hooks";
