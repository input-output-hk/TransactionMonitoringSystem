/**
 * Shared HTTP helpers for the backend API.
 *
 * - {@link fetchWithAuth} is a thin wrapper around `fetch()` that ensures
 *   the magic-link session cookie (`tms_session`, HTTP-only) is sent on
 *   every request. The SPA itself never reads or writes that cookie.
 * - {@link getNetwork} returns the active Cardano network (default `preprod`).
 *
 * Historical note: this module used to also inject a `TMS-API-Key`
 * header taken from the `VITE_TMS_API_KEY` build env. That key was
 * baked into the public JS bundle — anyone opening DevTools could read
 * it and call `/api/*` without going through the magic-link auth.
 * Removed once session cookies became the canonical browser credential.
 * The backend's `verify_api_key` still accepts an `API_KEYS` env for
 * server-to-server callers (CLI, integrations), but the SPA no longer
 * sends one.
 */

export const DEFAULT_NETWORK: "mainnet" | "preprod" | "preview" =
	(import.meta.env.VITE_NETWORK as "mainnet" | "preprod" | "preview") ??
	"preprod";

/** Active Cardano network for all backend calls. */
export function getNetwork(): "mainnet" | "preprod" | "preview" {
	return DEFAULT_NETWORK;
}

/**
 * `fetch()` with `credentials: "include"` so the session cookie rides
 * on every request. Same-origin dev (Vite proxy) would send cookies
 * by default anyway, but being explicit insulates us from a future
 * cross-origin deployment. Returns the raw `Response` — callers are
 * responsible for status checks and JSON parsing.
 */
export function fetchWithAuth(
	input: RequestInfo | URL,
	init?: RequestInit,
): Promise<Response> {
	return fetch(input, { ...init, credentials: "include" });
}
