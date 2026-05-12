import { useAuth } from "@/lib/auth-context";
import { Navigate, Outlet } from "react-router-dom";

export function RequireAuth() {
	const { isAuthenticated } = useAuth();
	if (!isAuthenticated) return <Navigate to="/signup" replace />;
	return <Outlet />;
}

export function RedirectIfAuthed() {
	const { isAuthenticated } = useAuth();
	if (isAuthenticated) return <Navigate to="/dashboard" replace />;
	return <Outlet />;
}
