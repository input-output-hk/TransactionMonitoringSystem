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
import { LogOut, User } from "lucide-react";
import { useId } from "react";

/* ---------- User-menu icons ----------
 *
 * Inlined Figma assets. The stroke-based ones use `currentColor` so they
 * follow the menu text color across themes; the moon and sun chips keep
 * their absolute Figma fills because they're "badges" by design (a pale
 * moon and a peach sun), not monochrome glyphs.
 *
 * Filter `id`s are namespaced to this component so they don't collide
 * with other SVG filters on the page.
 */

function UploadCloudIcon({ className }: { className?: string }) {
	return (
		<svg
			className={className}
			viewBox="0 0 20 20"
			fill="none"
			xmlns="http://www.w3.org/2000/svg"
			aria-hidden="true"
		>
			<path
				d="M6.6659 13.3332L9.99923 9.9999L13.3326 13.3332M9.99923 9.9999V17.4999M16.9909 15.3249C17.8037 14.8818 18.4458 14.1806 18.8158 13.3321C19.1858 12.4835 19.2627 11.5359 19.0344 10.6388C18.8061 9.7417 18.2855 8.94616 17.5548 8.37778C16.8241 7.80939 15.925 7.50052 14.9992 7.4999H13.9492C13.697 6.52427 13.2269 5.61852 12.5742 4.85073C11.9215 4.08295 11.1033 3.47311 10.181 3.06708C9.2587 2.66104 8.25636 2.46937 7.24933 2.50647C6.2423 2.54358 5.25679 2.80849 4.36688 3.28129C3.47697 3.7541 2.70583 4.42249 2.11142 5.23622C1.51701 6.04996 1.11481 6.98785 0.935051 7.9794C0.755293 8.97095 0.802655 9.99035 1.07358 10.961C1.3445 11.9316 1.83194 12.8281 2.49923 13.5832"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}

function ArchiveIcon({ className }: { className?: string }) {
	return (
		<svg
			className={className}
			viewBox="0 0 20 20"
			fill="none"
			xmlns="http://www.w3.org/2000/svg"
			aria-hidden="true"
		>
			<path
				d="M17.5007 6.66667V17.5H2.50065V6.66667M8.33398 10H11.6673M0.833984 2.5H19.1673V6.66667H0.833984V2.5Z"
				stroke="currentColor"
				strokeOpacity="0.5"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}

/** Filled crescent moon, mirrored so the cut-out faces right (matches the
 *  Figma "chip" intent). The original Figma asset was two overlapping
 *  circles that didn't render as a moon — this is a single clean path
 *  flipped via `transform="scale(-1,1)"` and filled with the same pale
 *  `#DEE5F3` from the original design so it reads as a moon badge. */
function DarkModeIcon({ className }: { className?: string }) {
	return (
		<svg
			className={className}
			viewBox="0 0 24 24"
			fill="none"
			xmlns="http://www.w3.org/2000/svg"
			aria-hidden="true"
		>
			<path
				d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"
				transform="translate(24 0) scale(-1 1)"
				fill="#DEE5F3"
				fillOpacity="0.9"
			/>
		</svg>
	);
}

/** Peach sun "chip" — fill colors are intentional (not currentColor). */
function LightModeIcon({ className }: { className?: string }) {
	// `useId` keeps the SVG filter id unique per render so two instances of
	// this icon in the same document (e.g. StrictMode double-render) don't
	// share — and accidentally swap — the same `<filter>`.
	const filterId = `tms-light-mode-shadow-${useId()}`;
	return (
		<svg
			className={className}
			viewBox="0 0 44 44"
			fill="none"
			xmlns="http://www.w3.org/2000/svg"
			aria-hidden="true"
		>
			<g filter={`url(#${filterId})`}>
				<rect
					width="20"
					height="20"
					rx="10"
					transform="matrix(-1 3.99602e-09 3.99602e-09 1 31.6992 11.7)"
					fill="#FFC187"
					fillOpacity="0.96"
				/>
			</g>
			<defs>
				<filter
					id={filterId}
					x="-0.000782013"
					y="-4.95911e-05"
					width="43.4"
					height="43.4"
					filterUnits="userSpaceOnUse"
					colorInterpolationFilters="sRGB"
				>
					<feFlood floodOpacity="0" result="BackgroundImageFix" />
					<feColorMatrix
						in="SourceAlpha"
						type="matrix"
						values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0"
						result="hardAlpha"
					/>
					<feOffset />
					<feGaussianBlur stdDeviation="5.85" />
					<feColorMatrix
						type="matrix"
						values="0 0 0 0 1 0 0 0 0 0.756863 0 0 0 0 0.529412 0 0 0 0.6 0"
					/>
					<feBlend
						mode="normal"
						in2="BackgroundImageFix"
						result="effect1_dropShadow"
					/>
					<feColorMatrix
						in="SourceAlpha"
						type="matrix"
						values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0"
						result="hardAlpha"
					/>
					<feOffset dx="-3.9" dy="6.5" />
					<feGaussianBlur stdDeviation="2.6" />
					<feColorMatrix
						type="matrix"
						values="0 0 0 0 0.717122 0 0 0 0 0.717122 0 0 0 0 0.717122 0 0 0 0.35 0"
					/>
					<feBlend
						mode="normal"
						in2="effect1_dropShadow"
						result="effect2_dropShadow"
					/>
					<feBlend
						mode="normal"
						in="SourceGraphic"
						in2="effect2_dropShadow"
						result="shape"
					/>
					<feColorMatrix
						in="SourceAlpha"
						type="matrix"
						values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0"
						result="hardAlpha"
					/>
					<feOffset dy="2.6" />
					<feGaussianBlur stdDeviation="2.6" />
					<feComposite
						in2="hardAlpha"
						operator="arithmetic"
						k2="-1"
						k3="1"
					/>
					<feColorMatrix
						type="matrix"
						values="0 0 0 0 1 0 0 0 0 0.816106 0 0 0 0 0.645833 0 0 0 1 0"
					/>
					<feBlend
						mode="normal"
						in2="shape"
						result="effect3_innerShadow"
					/>
					<feColorMatrix
						in="SourceAlpha"
						type="matrix"
						values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0"
						result="hardAlpha"
					/>
					<feOffset dy="-2.6" />
					<feGaussianBlur stdDeviation="2.6" />
					<feComposite
						in2="hardAlpha"
						operator="arithmetic"
						k2="-1"
						k3="1"
					/>
					<feColorMatrix
						type="matrix"
						values="0 0 0 0 1 0 0 0 0 0.631795 0 0 0 0 0.2875 0 0 0 1 0"
					/>
					<feBlend
						mode="normal"
						in2="effect3_innerShadow"
						result="effect4_innerShadow"
					/>
				</filter>
			</defs>
		</svg>
	);
}
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
					{health?.clustering_enabled && (
						<NavItem to="/validators">Validators</NavItem>
					)}
					{user?.role === "Admin" && <NavItem to="/users">Users</NavItem>}
					{user?.role === "Admin" && (
						<NavItem to="/settings/notifications">Notifications</NavItem>
					)}
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
										Network: {health.network}
										{health.ogmios && (
											<> · Lag {health.ogmios.sync_lag_seconds}s</>
										)}
									</DropdownMenuItem>
								</>
							)}
						</DropdownMenuContent>
					</DropdownMenu>

					<DropdownMenu>
						<DropdownMenuTrigger className="focus-visible:ring-ring focus-visible:ring-offset-background shrink-0 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-offset-2">
							<Avatar className="shrink-0">
								<AvatarFallback>{initials(user?.full_name)}</AvatarFallback>
							</Avatar>
						</DropdownMenuTrigger>
						<DropdownMenuContent align="end" className="min-w-56">
							<DropdownMenuItem className="gap-3" disabled>
								<User className="text-brand h-4 w-4" />
								<span className="text-foreground font-medium">
									{user?.full_name ?? "User"}
								</span>
							</DropdownMenuItem>
							<DropdownMenuSeparator />
							<DropdownMenuItem onSelect={toggleTheme} className="gap-3">
								{theme === "dark" ? (
									<DarkModeIcon className="h-4 w-4" />
								) : (
									<LightModeIcon className="h-4 w-4" />
								)}
								{theme === "dark" ? "Dark Mode On" : "Light Mode On"}
							</DropdownMenuItem>
							<DropdownMenuItem
								className="gap-3"
								onSelect={() => void navigate("/import")}
							>
								<UploadCloudIcon className="h-4 w-4" />
								Import Archive
							</DropdownMenuItem>
							<DropdownMenuItem
								className="gap-3"
								onSelect={() => void navigate("/archive")}
							>
								<ArchiveIcon className="h-4 w-4" />
								Archive
							</DropdownMenuItem>
							<DropdownMenuSeparator />
							<DropdownMenuItem
								onSelect={() => {
									void logout();
									void navigate("/login", { replace: true });
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
