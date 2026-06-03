/**
 * App-wide auth state, backed by `GET /api/auth/me`.
 *
 * Mounts once at the root of the tree (see `main.tsx`) and shares the
 * resolved user via context. Components that need auth state pull it
 * with `useAuth()` from `@/lib/auth`, which re-exports the hook here.
 *
 * TanStack Query handles caching + revalidation:
 * - `staleTime: 5min` — the user's role/email rarely change mid-session,
 *   so we avoid spamming `/me` on every navigation.
 * - `refetchOnWindowFocus: false` — defaults to true, which would
 *   reissue `/me` every time the tab is refocused. Not worth it for
 *   data that's effectively immutable per session.
 * - 401 is treated as "anonymous", not as an error — `fetchMe()`
 *   returns null in that case so the query resolves successfully with
 *   `user === null`.
 *
 * The provider also exposes `refetchUser()` (called after a successful
 * magic-link verify) and `logoutLocal()` (used by the top-nav menu so
 * the UI flips immediately, before the network round-trip completes).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, type ReactNode } from "react";
import { fetchMe, logout as apiLogout, type User } from "@/lib/api/auth";

type AuthContextValue = {
	user: User | null;
	/** True while the initial `/me` request hasn't resolved yet. */
	isLoading: boolean;
	/** True once `/me` has resolved (regardless of authenticated state). */
	isReady: boolean;
	/** Re-fetch `/me` — called after login completes via /auth/verify. */
	refetchUser: () => Promise<void>;
	/** Call `/api/auth/logout` and clear local user state. */
	logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

const ME_QUERY_KEY = ["auth", "me"] as const;

export function AuthProvider({ children }: { children: ReactNode }) {
	const qc = useQueryClient();

	const { data, isLoading, isFetched } = useQuery({
		queryKey: ME_QUERY_KEY,
		queryFn: fetchMe,
		staleTime: 5 * 60 * 1000,
		refetchOnWindowFocus: false,
		// Don't retry on 401 — fetchMe already returns null for that.
		retry: false,
	});

	const refetchUser = useCallback(async () => {
		await qc.invalidateQueries({ queryKey: ME_QUERY_KEY });
	}, [qc]);

	const logout = useCallback(async () => {
		try {
			await apiLogout();
		} finally {
			// Whether the server-side logout succeeded or not, drop our local
			// cache so the UI doesn't pretend the user is still signed in.
			qc.setQueryData(ME_QUERY_KEY, null);
		}
	}, [qc]);

	const value: AuthContextValue = {
		user: data ?? null,
		isLoading,
		isReady: isFetched,
		refetchUser,
		logout,
	};

	return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Access auth state from any component inside `<AuthProvider>`. */
export function useAuthContext(): AuthContextValue {
	const ctx = useContext(AuthContext);
	if (!ctx) {
		throw new Error(
			"useAuthContext must be used inside <AuthProvider> (wrap your app root)",
		);
	}
	return ctx;
}
