/**
 * Auth API client. Talks to `/api/auth/*` and `/api/users` on the backend.
 *
 * Session cookies (`tms_session`, HTTP-only) ride along automatically via
 * `fetchWithAuth({ credentials: "include" })` — the SPA never reads or
 * writes the cookie directly.
 *
 * The mapping below converts backend snake_case payloads
 * (``full_name``, ``created_at``) to the camelCase shape the rest of
 * the app expects. Keep it centralized so callers don't deal with both.
 */
import { fetchWithAuth } from "./fetch";

export type UserRole = "Admin" | "Reviewer";
export type UserStatus = "pending" | "active" | "disabled";

export type User = {
	id: string;
	email: string;
	fullName: string;
	role: UserRole;
	status: UserStatus;
	createdAt: string;
	lastLoginAt: string | null;
};

export type UsersListResponse = {
	count: number;
	total: number;
	data: User[];
};

type ApiUser = {
	id: string;
	email: string;
	full_name: string;
	role: UserRole;
	status: UserStatus;
	created_at: string;
	last_login_at: string | null;
};

function toUser(u: ApiUser): User {
	return {
		id: u.id,
		email: u.email,
		fullName: u.full_name,
		role: u.role,
		status: u.status,
		createdAt: u.created_at,
		lastLoginAt: u.last_login_at,
	};
}

/* ---------- /api/auth/* ---------- */

/**
 * Ask the backend to email a magic-link login to `email`. Returns void
 * regardless of whether the email exists — the backend opts not to leak
 * that signal. Throws only on network / 5xx errors.
 */
export async function requestLink(email: string): Promise<void> {
	const res = await fetchWithAuth("/api/auth/request-link", {
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
		`/api/auth/verify?token=${encodeURIComponent(token)}`,
	);
	if (!res.ok) {
		const err = (await res.json().catch(() => null)) as
			| { detail?: string }
			| null;
		throw new Error(err?.detail ?? `Verification failed (${res.status})`);
	}
	return toUser((await res.json()) as ApiUser);
}

/** Drops the current session server-side and clears the cookie. */
export async function logout(): Promise<void> {
	await fetchWithAuth("/api/auth/logout", { method: "POST" });
}

/**
 * Resolve the current session to a user. Returns `null` when the user
 * is anonymous (401). Throws on 5xx so the caller can render an error
 * boundary instead of silently treating the user as logged-out.
 */
export async function fetchMe(): Promise<User | null> {
	const res = await fetchWithAuth("/api/auth/me");
	if (res.status === 401) return null;
	if (!res.ok) {
		throw new Error(`me failed (${res.status})`);
	}
	return toUser((await res.json()) as ApiUser);
}

/* ---------- /api/users (admin) ---------- */

export async function listUsers(
	params: { limit?: number; offset?: number } = {},
): Promise<UsersListResponse> {
	const qs = new URLSearchParams();
	if (params.limit != null) qs.set("limit", String(params.limit));
	if (params.offset != null) qs.set("offset", String(params.offset));
	const url = `/api/users${qs.toString() ? `?${qs.toString()}` : ""}`;
	const res = await fetchWithAuth(url);
	if (!res.ok) {
		throw new Error(`list users failed (${res.status})`);
	}
	const json = (await res.json()) as {
		count: number;
		total: number;
		data: ApiUser[];
	};
	return {
		count: json.count,
		total: json.total,
		data: json.data.map(toUser),
	};
}

export type CreateUserPayload = {
	email: string;
	fullName: string;
	role: UserRole;
};

export async function createUser(payload: CreateUserPayload): Promise<User> {
	const res = await fetchWithAuth("/api/users", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({
			email: payload.email,
			full_name: payload.fullName,
			role: payload.role,
		}),
	});
	if (!res.ok) {
		const err = (await res.json().catch(() => null)) as
			| { detail?: string }
			| null;
		throw new Error(err?.detail ?? `create user failed (${res.status})`);
	}
	return toUser((await res.json()) as ApiUser);
}

export async function deleteUser(id: string): Promise<void> {
	const res = await fetchWithAuth(`/api/users/${encodeURIComponent(id)}`, {
		method: "DELETE",
	});
	// 204 No Content is the happy path (`res.ok` already covers it). Other
	// statuses come back as JSON with a `detail` (e.g. 400 "cannot delete
	// your own account", 404, 403).
	if (!res.ok) {
		const err = (await res.json().catch(() => null)) as
			| { detail?: string }
			| null;
		throw new Error(err?.detail ?? `delete user failed (${res.status})`);
	}
}

export async function resendInvite(id: string): Promise<void> {
	const res = await fetchWithAuth(
		`/api/users/${encodeURIComponent(id)}/resend-invite`,
		{ method: "POST" },
	);
	if (!res.ok) {
		const err = (await res.json().catch(() => null)) as
			| { detail?: string }
			| null;
		throw new Error(err?.detail ?? `resend invite failed (${res.status})`);
	}
}
