import { useAuth, useAuthStore } from "@/lib/auth";
import { LogIn, LogOut } from "lucide-react";

export function DevAuthBar() {
	const { isAuthenticated } = useAuth();
	if (!import.meta.env.DEV) return null;

	const skipAuth = () => {
		useAuthStore.setState({
			user: {
				fullName: "Dev User",
				email: "dev@example.com",
				role: "Admin",
			},
			verified: true,
		});
		window.location.href = "/dashboard";
	};

	const devLogout = () => {
		useAuthStore.getState().logout();
		window.location.href = "/signup";
	};

	return (
		<div className="border-border bg-card/90 text-muted-foreground fixed right-3 bottom-3 z-[60] flex items-center gap-2 rounded-full border px-2 py-1 text-xs shadow-lg backdrop-blur">
			<span className="bg-brand/15 text-brand rounded-sm px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wider uppercase">
				Dev
			</span>
			{isAuthenticated ? (
				<button
					type="button"
					onClick={devLogout}
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
