// Same-origin transport for the clustering reverse-proxy. Internal to the
// client (the barrel does not re-export these); hooks call get/send.
import { fetchWithAuth } from "../fetch";
import type { Validator } from "./validation";

export const BASE = "/api/v1/clustering";

/** The server-side maximum page size (`limit` is capped at le=1000). List hooks
 *  that want the whole collection request one page at this limit so the API's
 *  default page of 100 can never silently truncate what the UI shows. */
export const MAX_PAGE_LIMIT = 1000;

export const HTTP_FORBIDDEN = 403;

/**
 * What a 403 on a clustering mutation actually means: the host proxy gates all
 * POST/PATCH/DELETE behind `require_admin_or_api_key` (backend
 * `app/api/clustering.py`), so a Reviewer session is rejected by role, not
 * because the request was malformed. Surfaced verbatim so error UIs don't
 * mislead a read-only user into "fixing" valid parameters.
 */
export const PERMISSION_DENIED_MESSAGE =
	"Your account has read-only access to clustering. Running this action requires an Admin account.";

/** Typed HTTP failure from the clustering proxy, carrying the status so
 *  callers can branch (e.g. permission vs. parameter/server errors). */
export class ClusteringApiError extends Error {
	readonly status: number;
	/** True only for the mutation role gate (a 403 from `send`). A 403 from a
	 *  read is a service misconfig, not a role rejection, so it stays false;
	 *  this is the signal `isPermissionDenied` keys on, not the bare status. */
	readonly permissionDenied: boolean;

	constructor(message: string, status: number, permissionDenied = false) {
		super(message);
		this.name = "ClusteringApiError";
		this.status = status;
		this.permissionDenied = permissionDenied;
	}
}

/** True when `err` is the proxy's role rejection (Admin-only mutation
 *  attempted by a read-only session). Deliberately keys on the mutation-only
 *  `permissionDenied` flag, not on `status === 403`, so a 403 from a read
 *  (sidecar key mismatch, etc.) is not misreported as a role problem. */
export function isPermissionDenied(err: unknown): err is ClusteringApiError {
	return err instanceof ClusteringApiError && err.permissionDenied;
}

export async function get<T>(
	path: string,
	validate?: Validator<T>,
): Promise<T> {
	const res = await fetchWithAuth(`${BASE}${path}`);
	// Reads are open to any authenticated principal, so a 403 here is service
	// misconfiguration (e.g. sidecar key mismatch), not a role problem: keep
	// the raw status message and leave permissionDenied false.
	if (!res.ok)
		throw new ClusteringApiError(
			`clustering ${path} failed: ${res.status}`,
			res.status,
		);
	const raw: unknown = await res.json();
	return validate ? validate(raw) : (raw as T);
}

export async function send<T>(
	method: string,
	path: string,
	body?: unknown,
	validate?: Validator<T>,
): Promise<T> {
	const res = await fetchWithAuth(`${BASE}${path}`, {
		method,
		headers: { "Content-Type": "application/json" },
		body: body === undefined ? undefined : JSON.stringify(body),
	});
	if (!res.ok) {
		const denied = res.status === HTTP_FORBIDDEN;
		throw new ClusteringApiError(
			denied
				? PERMISSION_DENIED_MESSAGE
				: `clustering ${method} ${path} failed: ${res.status}`,
			res.status,
			denied,
		);
	}
	const raw: unknown = await res.json();
	return validate ? validate(raw) : (raw as T);
}
