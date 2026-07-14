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
 * Historical note: this module used to also inject an API-key
 * header taken from the `VITE_TMS_API_KEY` build env. That key was
 * baked into the public JS bundle — anyone opening DevTools could read
 * it and call `/api/v1/*` without going through the magic-link auth.
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

// Must match the backend's frozen CSRF_COOKIE_NAME / CSRF_HEADER_NAME
// constants (backend/app/csrf.py) — they are deliberately not configurable
// there precisely so these strings cannot drift.
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
 * Thrown by {@link fetchWithAuth} when the backend answers 401 on an
 * endpoint that expects an authenticated session. The QueryClient's
 * cache-level `onError` (see `main.tsx`) catches this and flips the
 * cached auth state to anonymous, so `RequireAuth` redirects to /login
 * instead of leaving the page stuck on a failed query.
 */
export class UnauthorizedError extends Error {
	constructor(url: string) {
		super(`Unauthorized (401): ${url}`);
		this.name = "UnauthorizedError";
	}
}

/**
 * `fetch()` with `credentials: "include"` so the session cookie rides
 * on every request. Same-origin dev (Vite proxy) would send cookies
 * by default anyway, but being explicit insulates us from a future
 * cross-origin deployment. On a mutating request, echoes the CSRF cookie
 * back as a header (the backend rejects a mismatch/missing header —
 * see app.csrf.CSRFMiddleware). Returns the raw `Response` — callers are
 * responsible for status checks and JSON parsing.
 *
 * A 401 response throws {@link UnauthorizedError} instead of returning,
 * so an expired session surfaces as a typed error wherever it happens.
 * Callers for which 401 is a normal domain outcome (only `fetchMe`,
 * which maps it to "anonymous") opt out with `allow401: true`.
 */
export async function fetchWithAuth(
	input: RequestInfo | URL,
	init?: RequestInit,
	opts?: { allow401?: boolean },
): Promise<Response> {
	const method = (init?.method ?? "GET").toUpperCase();
	const headers = new Headers(init?.headers);
	if (MUTATING_METHODS.has(method)) {
		const csrfToken = readCookie(CSRF_COOKIE_NAME);
		if (csrfToken) {
			headers.set(CSRF_HEADER_NAME, csrfToken);
		}
	}
	const res = await fetch(input, { ...init, headers, credentials: "include" });
	if (res.status === 401 && !opts?.allow401) {
		const url =
			typeof input === "string"
				? input
				: input instanceof URL
					? input.href
					: input.url;
		throw new UnauthorizedError(url);
	}
	return res;
}
