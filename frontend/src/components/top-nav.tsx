import { NavLink, useNavigate } from "react-router-dom";
import { Archive, LogOut, Moon, Sun, Upload, User } from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuLabel,
	DropdownMenuSeparator,
	DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/lib/auth";
import { deriveModules, useHealth } from "@/lib/api/health";
import { useTheme } from "@/components/theme-context";
import { cn } from "@/lib/utils";

function initials(name: string | undefined) {
	if (!name) return "U";
	return name
		.split(/\s+/)
		.filter(Boolean)
		.slice(0, 2)
		.map((p) => p[0]?.toUpperCase())
		.join("");
}

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
		<header className="border-border bg-card border-b">
			<div className="mx-auto flex h-14 max-w-[1400px] items-center justify-between px-6">
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
						<DropdownMenuTrigger className="text-foreground hover:bg-accent focus-visible:ring-ring flex items-center gap-2 rounded-md px-2 py-1 text-sm font-medium transition-colors outline-none focus-visible:ring-2">
							<span
								className={cn(
									"h-2.5 w-2.5 rounded-full",
									overall === "online" && "bg-status-online",
									overall === "warning" && "bg-status-warning",
									overall === "down" && "bg-status-offline",
									overall === "loading" && "bg-muted-foreground",
								)}
							/>
							System Status
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
									<DropdownMenuItem disabled className="text-muted-foreground gap-3 text-[11px]">
										Network: {health.network} · Lag {health.ogmios.sync_lag_seconds}s
									</DropdownMenuItem>
								</>
							)}
						</DropdownMenuContent>
					</DropdownMenu>

					<DropdownMenu>
						<DropdownMenuTrigger className="focus-visible:ring-ring focus-visible:ring-offset-background rounded-full outline-none focus-visible:ring-2 focus-visible:ring-offset-2">
							<Avatar>
								<AvatarFallback>{initials(user?.fullName)}</AvatarFallback>
							</Avatar>
						</DropdownMenuTrigger>
						<DropdownMenuContent align="end" className="min-w-[14rem]">
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
								Import Attack
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
									logout();
									navigate("/signup", { replace: true });
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
