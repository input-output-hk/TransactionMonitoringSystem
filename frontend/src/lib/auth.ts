/**
 * Auth surface re-exported from the AuthProvider context.
 *
 * Historical note: this module used to be a Zustand-backed mock with
 * `signUp` / `verifyEmail` / `resendEmail` actions. After the
 * magic-link backend landed (Phase 2/3), all of that was replaced by:
 *
 *  - a real session cookie set by `GET /api/v1/auth/verify?token=…`
 *  - the `<AuthProvider>` context built on a TanStack Query against
 *    `GET /api/v1/auth/me` (see `components/auth-provider.tsx`)
 *
 * Callers should keep using `useAuth()` from here — the hook's return
 * shape is intentionally kept close to the old mock so the migration
 * stays low-friction. The `signUp` / `verifyEmail` actions are gone
 * (no self-signup, verification happens via the URL token), and have
 * been replaced by `requestLink(email)` in `@/lib/api/auth`.
 */
import { useAuthContext } from "@/components/auth-context";
import type { User } from "@/lib/api/auth";

export type { User };

/** Hook used everywhere in the app to read auth state. */
export function useAuth() {
	const { user, isLoading, isReady, isError, logout, refetchUser } =
		useAuthContext();
	return {
		user,
		/** True only when /me resolved AND returned a user. */
		isAuthenticated: user !== null,
		/** True when the signed-in user has the Admin role. Reviewer sessions
		 *  are read-only for mutating surfaces (e.g. clustering actions); UIs
		 *  use this to disable those controls up front instead of letting the
		 *  request fail with a 403. */
		isAdmin: user?.role === "Admin",
		/** Initial /me request still pending. Route guards should wait on this. */
		isLoading,
		/** /me has resolved at least once (regardless of result). */
		isReady,
		/** /me failed with a non-401 error (5xx / network). */
		isError,
		logout,
		refetchUser,
	};
}
