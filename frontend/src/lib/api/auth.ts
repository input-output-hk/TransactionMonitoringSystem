/**
 * Auth API client. Talks to `/api/v1/auth/*` and `/api/v1/users` on the backend.
 *
 * Session cookies (`tms_session`, HTTP-only) ride along automatically via
 * `fetchWithAuth({ credentials: "include" })` — the SPA never reads or
 * writes the cookie directly.
 *
 * Payloads stay snake_case end to end, matching the backend and every other
 * API module (see lib/api/README.md): `User` mirrors the backend field names
 * verbatim, so there is no per-module case-mapping layer to keep in sync.
 */
import { fetchWithAuth } from "./fetch";

/**
 * Cache key for the resolved `/api/v1/auth/me` user. Shared between the
 * AuthProvider (which owns the query) and the QueryClient's global 401
 * handler in `main.tsx` (which resets it to anonymous).
 */
export const ME_QUERY_KEY = ["auth", "me"] as const;

export type UserRole = "Admin" | "Reviewer";
export type UserStatus = "pending" | "active" | "disabled";

export type User = {
	id: string;
	email: string;
	full_name: string;
	role: UserRole;
	status: UserStatus;
	created_at: string;
	last_login_at: string | null;
};

export type UsersListResponse = {
	count: number;
	total: number;
	data: User[];
};

/* ---------- /api/v1/auth/* ---------- */

/**
 * Ask the backend to email a magic-link login to `email`. Returns void
 * regardless of whether the email exists — the backend opts not to leak
 * that signal. Throws only on network / 5xx errors.
 */
export async function requestLink(email: string): Promise<void> {
	const res = await fetchWithAuth("/api/v1/auth/request-link", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ email }),
	});
	if (!res.ok) {
		const body = await res.text().catch(() => "");
		throw new Error(`request-link failed (${res.status}): ${body}`);
	}
}

/**
 * Redeem a magic-link token. On success the backend sets the session
 * cookie and returns the user; on invalid/expired token it returns 400.
 */
export async function verifyToken(token: string): Promise<User> {
	const res = await fetchWithAuth(
		`/api/v1/auth/verify?token=${encodeURIComponent(token)}`,
	);
	if (!res.ok) {
		const err = (await res.json().catch(() => null)) as {
			detail?: string;
		} | null;
		throw new Error(err?.detail ?? `Verification failed (${res.status})`);
	}
	return (await res.json()) as User;
}

/** Drops the current session server-side and clears the cookie. */
export async function logout(): Promise<void> {
	await fetchWithAuth("/api/v1/auth/logout", { method: "POST" });
}

/**
 * Resolve the current session to a user. Returns `null` when the user
 * is anonymous (401). Throws on 5xx so the caller can render an error
 * boundary instead of silently treating the user as logged-out.
 */
export async function fetchMe(): Promise<User | null> {
	// allow401: anonymous is a normal outcome here, not a session expiry —
	// mapping it to null (instead of UnauthorizedError) keeps the login
	// page reachable.
	const res = await fetchWithAuth("/api/v1/auth/me", undefined, {
		allow401: true,
	});
	if (res.status === 401) return null;
	if (!res.ok) {
		throw new Error(`me failed (${res.status})`);
	}
	return (await res.json()) as User;
}

/* ---------- /api/v1/users (admin) ---------- */

export async function listUsers(
	params: { limit?: number; offset?: number } = {},
): Promise<UsersListResponse> {
	const qs = new URLSearchParams();
	if (params.limit != null) qs.set("limit", String(params.limit));
	if (params.offset != null) qs.set("offset", String(params.offset));
	const url = `/api/v1/users${qs.toString() ? `?${qs.toString()}` : ""}`;
	const res = await fetchWithAuth(url);
	if (!res.ok) {
		throw new Error(`list users failed (${res.status})`);
	}
	return (await res.json()) as UsersListResponse;
}

export type CreateUserPayload = {
	email: string;
	full_name: string;
	role: UserRole;
};

export async function createUser(payload: CreateUserPayload): Promise<User> {
	const res = await fetchWithAuth("/api/v1/users", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(payload),
	});
	if (!res.ok) {
		const err = (await res.json().catch(() => null)) as {
			detail?: string;
		} | null;
		throw new Error(err?.detail ?? `create user failed (${res.status})`);
	}
	return (await res.json()) as User;
}

export async function updateUser(
	id: string,
	payload: { role: UserRole },
): Promise<User> {
	// PATCH is a mutating method, so fetchWithAuth attaches the CSRF header
	// automatically. Non-2xx come back as JSON with a `detail` (e.g. 400
	// "cannot change your own role" / "last active Admin", 403, 404).
	const res = await fetchWithAuth(`/api/v1/users/${encodeURIComponent(id)}`, {
		method: "PATCH",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(payload),
	});
	if (!res.ok) {
		const err = (await res.json().catch(() => null)) as {
			detail?: string;
		} | null;
		throw new Error(err?.detail ?? `update user failed (${res.status})`);
	}
	return (await res.json()) as User;
}

export async function deleteUser(id: string): Promise<void> {
	const res = await fetchWithAuth(`/api/v1/users/${encodeURIComponent(id)}`, {
		method: "DELETE",
	});
	// 204 No Content is the happy path (`res.ok` already covers it). Other
	// statuses come back as JSON with a `detail` (e.g. 400 "cannot delete
	// your own account", 404, 403).
	if (!res.ok) {
		const err = (await res.json().catch(() => null)) as {
			detail?: string;
		} | null;
		throw new Error(err?.detail ?? `delete user failed (${res.status})`);
	}
}

export async function resendInvite(id: string): Promise<void> {
	const res = await fetchWithAuth(
		`/api/v1/users/${encodeURIComponent(id)}/resend-invite`,
		{ method: "POST" },
	);
	if (!res.ok) {
		const err = (await res.json().catch(() => null)) as {
			detail?: string;
		} | null;
		throw new Error(err?.detail ?? `resend invite failed (${res.status})`);
	}
}
