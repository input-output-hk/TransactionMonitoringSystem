import { useTheme } from "@/components/theme-context";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuLabel,
	DropdownMenuSeparator,
	DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { deriveModules, useHealth } from "@/lib/api/health";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { initials } from "@/lib/utils/strings";
import { Archive, LogOut, Moon, Sun, Upload, User } from "lucide-react";
import { NavLink, useNavigate } from "react-router-dom";

export function TopNav() {
	const navigate = useNavigate();
	const { user, logout } = useAuth();
	const { theme, toggleTheme } = useTheme();
	const { data: health, isError: healthError } = useHealth();
	const modules = deriveModules(health);
	const overall = healthError
		? "down"
		: !health
			? "loading"
			: modules.every((m) => m.online)
				? "online"
				: "warning";

	return (
		<header className="border-border bg-card">
			<div className="mx-auto flex h-14 max-w-350 items-center justify-between px-6">
				<nav className="flex items-center gap-1">
					<span className="text-foreground mr-3 text-base font-extrabold tracking-tight">
						TMS
					</span>
					<span className="text-border mr-3">|</span>
					<NavItem to="/dashboard">Attacks</NavItem>
					<NavItem to="/reports">Reports</NavItem>
					{user?.role === "Admin" && <NavItem to="/users">Users</NavItem>}
				</nav>

				<div className="flex items-center gap-5">
					<DropdownMenu>
						<DropdownMenuTrigger
							className="text-foreground hover:bg-accent focus-visible:ring-ring flex shrink-0 items-center gap-2 rounded-md px-2 py-1 text-sm font-medium whitespace-nowrap transition-colors outline-none focus-visible:ring-2"
							// Keep the dot tappable even when the label is hidden.
							aria-label="System Status"
						>
							<span
								className={cn(
									"h-2.5 w-2.5 shrink-0 rounded-full",
									overall === "online" && "bg-status-online",
									overall === "warning" && "bg-status-warning",
									overall === "down" && "bg-status-offline",
									overall === "loading" && "bg-muted-foreground",
								)}
							/>
							{/* Label hidden under the `md` breakpoint (~768px) — the
							    coloured dot keeps signalling status, and the dropdown
							    stays usable by clicking the dot. */}
							<span className="hidden md:inline">System Status</span>
						</DropdownMenuTrigger>
						<DropdownMenuContent align="end">
							<DropdownMenuLabel>Modules</DropdownMenuLabel>
							{modules.length === 0 && (
								<DropdownMenuItem disabled className="gap-3 text-xs">
									{healthError ? "Backend unreachable" : "Loading…"}
								</DropdownMenuItem>
							)}
							{modules.map((m) => (
								<DropdownMenuItem key={m.name} className="gap-3">
									<span
										className={cn(
											"h-2.5 w-2.5 rounded-full",
											m.online ? "bg-status-online" : "bg-status-offline",
										)}
									/>
									{m.name}
								</DropdownMenuItem>
							))}
							{health && (
								<>
									<DropdownMenuSeparator />
									<DropdownMenuItem
										disabled
										className="text-muted-foreground gap-3 text-[11px]"
									>
										Network: {health.network} · Lag{" "}
										{health.ogmios.sync_lag_seconds}s
									</DropdownMenuItem>
								</>
							)}
						</DropdownMenuContent>
					</DropdownMenu>

					<DropdownMenu>
						<DropdownMenuTrigger className="focus-visible:ring-ring focus-visible:ring-offset-background shrink-0 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-offset-2">
							<Avatar className="shrink-0">
								<AvatarFallback>{initials(user?.fullName)}</AvatarFallback>
							</Avatar>
						</DropdownMenuTrigger>
						<DropdownMenuContent align="end" className="min-w-56">
							<DropdownMenuItem className="gap-3" disabled>
								<User className="text-brand h-4 w-4" />
								<span className="text-foreground font-medium">
									{user?.fullName ?? "User"}
								</span>
							</DropdownMenuItem>
							<DropdownMenuSeparator />
							<DropdownMenuItem onSelect={toggleTheme} className="gap-3">
								{theme === "dark" ? (
									<Moon className="h-4 w-4" />
								) : (
									<Sun className="h-4 w-4" />
								)}
								{theme === "dark" ? "Dark Mode On" : "Light Mode On"}
							</DropdownMenuItem>
							<DropdownMenuItem
								className="gap-3"
								onSelect={() => navigate("/import")}
							>
								<Upload className="h-4 w-4" />
								Import Archive
							</DropdownMenuItem>
							<DropdownMenuItem
								className="gap-3"
								onSelect={() => navigate("/archive")}
							>
								<Archive className="h-4 w-4" />
								Archive
							</DropdownMenuItem>
							<DropdownMenuSeparator />
							<DropdownMenuItem
								onSelect={() => {
									void logout();
									navigate("/login", { replace: true });
								}}
								className="justify-end gap-3"
							>
								Log Out
								<LogOut className="h-4 w-4" />
							</DropdownMenuItem>
						</DropdownMenuContent>
					</DropdownMenu>
				</div>
			</div>
		</header>
	);
}

function NavItem({ to, children }: { to: string; children: React.ReactNode }) {
	return (
		<NavLink
			to={to}
			className={({ isActive }) =>
				cn(
					"rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
					isActive
						? "text-foreground"
						: "text-muted-foreground hover:text-foreground",
				)
			}
		>
			{children}
		</NavLink>
	);
}
