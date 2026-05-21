/**
 * Shared HTTP helpers for the backend API.
 *
 * - {@link fetchWithAuth} wraps the browser `fetch` and injects the
 *   `TMS-API-Key` header when {@link VITE_TMS_API_KEY} is configured.
 *   In dev (no key set) it's a no-op so the backend's dev-mode auth bypass
 *   keeps working.
 * - {@link getNetwork} returns the active Cardano network (default `preprod`).
 *
 * All API modules under `lib/api/*` should go through these helpers so that
 * production builds attach the API key consistently. Direct `fetch()` calls
 * will 403 against a backend with `API_KEYS` configured.
 */

const API_KEY_HEADER = "TMS-API-Key";
const API_KEY = import.meta.env.VITE_TMS_API_KEY?.trim() || undefined;

export const DEFAULT_NETWORK: "mainnet" | "preprod" | "preview" =
	(import.meta.env.VITE_NETWORK as "mainnet" | "preprod" | "preview") ??
	"preprod";

/** Active Cardano network for all backend calls. */
export function getNetwork(): "mainnet" | "preprod" | "preview" {
	return DEFAULT_NETWORK;
}

/**
 * Drop-in replacement for `fetch()` that attaches the API key header when
 * {@link VITE_TMS_API_KEY} is configured. Returns the raw `Response` — callers
 * are responsible for status checks and JSON parsing.
 */
export function fetchWithAuth(
	input: RequestInfo | URL,
	init?: RequestInit,
): Promise<Response> {
	if (!API_KEY) return fetch(input, init);

	// Merge in the API key header without disturbing any caller-set headers
	// (e.g. Content-Type on POST bodies).
	const headers = new Headers(init?.headers);
	if (!headers.has(API_KEY_HEADER)) {
		headers.set(API_KEY_HEADER, API_KEY);
	}
	return fetch(input, { ...init, headers });
}
