/**
 * Shared HTTP helpers for the backend API.
 *
 * - {@link fetchWithAuth} is a thin wrapper around `fetch()` that ensures
 *   the magic-link session cookie (`tms_session`, HTTP-only) is sent on
 *   every request, and echoes the CSRF double-submit cookie (`tms_csrf`,
 *   JS-readable) back as a header on mutating requests. The SPA never
 *   reads or writes the session cookie itself.
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

// Must match the backend's CSRF_COOKIE_NAME / CSRF_HEADER_NAME defaults
// (app/config.py) — see app.csrf.CSRFMiddleware.
const CSRF_COOKIE_NAME = "tms_csrf";
const CSRF_HEADER_NAME = "X-CSRF-Token";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Read a single cookie's value by name, or null if absent. Cookies with
 * this name never contain `=` or `;` (a URL-safe token), so no decoding
 * beyond a plain split is needed. */
function readCookie(name: string): string | null {
	const match = document.cookie
		.split("; ")
		.find((row) => row.startsWith(`${name}=`));
	return match ? match.slice(name.length + 1) : null;
}

/**
 * `fetch()` with `credentials: "include"` so the session cookie rides
 * on every request. Same-origin dev (Vite proxy) would send cookies
 * by default anyway, but being explicit insulates us from a future
 * cross-origin deployment. On a mutating request, echoes the CSRF cookie
 * back as a header (the backend rejects a mismatch/missing header —
 * see app.csrf.CSRFMiddleware). Returns the raw `Response` — callers are
 * responsible for status checks and JSON parsing.
 */
export function fetchWithAuth(
	input: RequestInfo | URL,
	init?: RequestInit,
): Promise<Response> {
	const method = (init?.method ?? "GET").toUpperCase();
	if (!MUTATING_METHODS.has(method)) {
		return fetch(input, { ...init, credentials: "include" });
	}
	const csrfToken = readCookie(CSRF_COOKIE_NAME);
	const headers = new Headers(init?.headers);
	if (csrfToken) {
		headers.set(CSRF_HEADER_NAME, csrfToken);
	}
	return fetch(input, { ...init, headers, credentials: "include" });
}
