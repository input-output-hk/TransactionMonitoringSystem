/**
 * Auth context + accessor hook, split out from `auth-provider.tsx` so that the
 * provider file exports only its component (Fast Refresh requires a file to
 * export components exclusively). `<AuthProvider>` supplies the value; consumers
 * read it via `useAuth()` in `@/lib/auth`, which wraps `useAuthContext` here.
 */
import { createContext, useContext } from "react";
import type { User } from "@/lib/api/auth";

export type AuthContextValue = {
	user: User | null;
	/** True while the initial `/me` request hasn't resolved yet. */
	isLoading: boolean;
	/** True once `/me` has resolved (regardless of authenticated state). */
	isReady: boolean;
	/**
	 * True when `/me` failed with a non-401 error (network down, 5xx).
	 * Distinguishes "anonymous user" (`user === null`, `isError === false`)
	 * from "we don't know yet because the backend is misbehaving"
	 * (`user === null`, `isError === true`). Route guards branch on this
	 * so a transient 5xx doesn't silently redirect to /login.
	 */
	isError: boolean;
	/** Re-fetch `/me`: called after login completes via /auth/verify. */
	refetchUser: () => Promise<void>;
	/** Call `/api/auth/logout` and clear local user state. */
	logout: () => Promise<void>;
};

export const AuthContext = createContext<AuthContextValue | null>(null);

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
