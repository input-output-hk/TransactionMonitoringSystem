/**
 * Route guards that wait on the AuthProvider's initial `/me` resolution
 * before deciding to redirect. Without the `isReady` check, the very
 * first render would flash a redirect (user appears anonymous because
 * the query hasn't returned yet) and the URL would bounce.
 */
import { useAuth } from "@/lib/auth";
import { Navigate, Outlet } from "react-router-dom";

function AuthBootstrapSpinner() {
	// Intentionally minimal — we expect /me to return in < 50ms on the
	// same-origin Vite proxy; this is just a no-flicker placeholder.
	return (
		<div className="bg-background text-muted-foreground flex min-h-screen items-center justify-center text-sm">
			Loading…
		</div>
	);
}

/** Renders the protected outlet only for authenticated users. */
export function RequireAuth() {
	const { isAuthenticated, isReady } = useAuth();
	if (!isReady) return <AuthBootstrapSpinner />;
	if (!isAuthenticated) return <Navigate to="/login" replace />;
	return <Outlet />;
}

/** For public routes (login, verify) — kicks already-signed-in users
 *  back to the dashboard so they can't see the login form. */
export function RedirectIfAuthed() {
	const { isAuthenticated, isReady } = useAuth();
	if (!isReady) return <AuthBootstrapSpinner />;
	if (isAuthenticated) return <Navigate to="/dashboard" replace />;
	return <Outlet />;
}

/** Admin-only routes (e.g. /users). Non-admins get silently redirected
 *  to /dashboard rather than a 403 page — they don't need to know the
 *  route exists, and the top nav already hides the link for them.
 *
 *  The backend still enforces ``require_admin`` on the API; this guard
 *  exists purely to prevent a non-admin from typing the URL and seeing
 *  a "Failed to load users" spinner before the 403 lands. */
export function RequireAdmin() {
	const { user, isReady } = useAuth();
	if (!isReady) return <AuthBootstrapSpinner />;
	if (!user) return <Navigate to="/login" replace />;
	if (user.role !== "Admin") return <Navigate to="/dashboard" replace />;
	return <Outlet />;
}
