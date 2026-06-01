import { useAuth, useAuthStore } from "@/lib/auth";
import { LogIn, LogOut } from "lucide-react";

/**
 * Floating "Demo" chip with a one-click skip-auth shortcut.
 *
 * Auth is currently a client-side mock (Zustand + localStorage), so the
 * "real" flow is signup → verify-email → dashboard. This bar bypasses
 * it for convenience. Exposed in BOTH dev and prod for now because we
 * don't have a real backend-backed login yet — remove the gate the same
 * day a real auth provider lands.
 */
export function DevAuthBar() {
	const { isAuthenticated } = useAuth();

	const skipAuth = () => {
		useAuthStore.setState({
			user: {
				fullName: "Demo User",
				email: "demo@example.com",
				role: "Admin",
			},
			verified: true,
		});
		window.location.href = "/dashboard";
	};

	const quickLogout = () => {
		useAuthStore.getState().logout();
		window.location.href = "/signup";
	};

	return (
		<div className="border-border bg-card/90 text-muted-foreground fixed right-3 bottom-3 z-[60] flex items-center gap-2 rounded-full border px-2 py-1 text-xs shadow-lg backdrop-blur">
			<span className="bg-brand/15 text-brand rounded-sm px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wider uppercase">
				Demo
			</span>
			{isAuthenticated ? (
				<button
					type="button"
					onClick={quickLogout}
					className="text-foreground hover:bg-accent inline-flex items-center gap-1.5 rounded-full px-2 py-1"
				>
					<LogOut className="h-3.5 w-3.5" />
					Logout
				</button>
			) : (
				<button
					type="button"
					onClick={skipAuth}
					className="text-foreground hover:bg-accent inline-flex items-center gap-1.5 rounded-full px-2 py-1"
				>
					<LogIn className="h-3.5 w-3.5" />
					Skip auth
				</button>
			)}
		</div>
	);
}
