import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
	AlertTriangle,
	ArrowUp,
	ChevronLeft,
	ChevronRight,
	ChevronsLeft,
	ChevronsRight,
	Copy,
	AlertCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import {
	ATTACK_TYPES,
	criticalAlertIdLong,
	latestBlocks,
	latestTransactions,
} from "@/mocks/attacks";
import { useActiveAlerts } from "@/lib/archive-store";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import { cn } from "@/lib/utils";

const SECONDARY_INFOS = [
	{ label: "TX / min", value: "12345" },
	{ label: "Pending", value: "12345" },
	{ label: "Critical (24h)", value: "12345" },
	{ label: "Avg Risk", value: "12345" },
];

export function AttacksPage() {
	const navigate = useNavigate();
	const activeAlerts = useActiveAlerts();
	const [attackFilter, setAttackFilter] = useState<string>("all");
	const [severityFilter, setSeverityFilter] = useState<string>("all");

	const filtered = useMemo(() => {
		return activeAlerts.filter((a) => {
			if (attackFilter !== "all" && a.attackType !== attackFilter) return false;
			if (severityFilter !== "all" && a.severity !== severityFilter)
				return false;
			return true;
		});
	}, [activeAlerts, attackFilter, severityFilter]);

	return (
		<div className="flex flex-col gap-4">
			{/* Top KPI row */}
			<div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-7">
				<CriticalAlertCard />
				{SECONDARY_INFOS.map((info) => (
					<KpiCard key={info.label} label={info.label} value={info.value} />
				))}
				<GraphBarCard />
			</div>

			{/* Risk Alerts */}
			<section className="border-border bg-card rounded-xl border">
				<header className="border-border flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3">
					<h2 className="text-foreground text-base font-semibold">
						Risk Alerts
					</h2>
					<div className="flex items-center gap-2">
						<Select value={attackFilter} onValueChange={setAttackFilter}>
							<SelectTrigger className="h-8 w-[160px]">
								<SelectValue placeholder="Attack Type" />
							</SelectTrigger>
							<SelectContent>
								<SelectItem value="all">All attack types</SelectItem>
								{ATTACK_TYPES.map((t) => (
									<SelectItem key={t} value={t}>
										{t}
									</SelectItem>
								))}
							</SelectContent>
						</Select>
						<Select value={severityFilter} onValueChange={setSeverityFilter}>
							<SelectTrigger className="h-8 w-[160px]">
								<SelectValue placeholder="Severity Type" />
							</SelectTrigger>
							<SelectContent>
								<SelectItem value="all">All severities</SelectItem>
								<SelectItem value="LOW">Low</SelectItem>
								<SelectItem value="MEDIUM">Medium</SelectItem>
								<SelectItem value="HIGH">High</SelectItem>
								<SelectItem value="CRITICAL">Critical</SelectItem>
							</SelectContent>
						</Select>
					</div>
				</header>

				<Table>
					<TableHeader>
						<TableRow className="hover:bg-transparent">
							<TableHead className="w-[42%]">ID</TableHead>
							<TableHead>Date</TableHead>
							<TableHead>Attack Type</TableHead>
							<TableHead className="pr-6 text-right">Severity</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{filtered.map((a) => {
							const Icon = ATTACK_ICON[a.attackType] ?? AlertCircle;
							return (
								<TableRow
									key={a.slug}
									onClick={() => navigate(`/attacks/${a.slug}`)}
									className="cursor-pointer"
								>
									<TableCell>
										<div className="text-foreground flex items-center gap-2 font-mono text-[13px]">
											<span>{a.id}</span>
											<button
												type="button"
												className="text-muted-foreground hover:text-foreground"
												title="Copy"
												onClick={(e) => {
													e.stopPropagation();
													navigator.clipboard?.writeText(a.id);
												}}
											>
												<Copy className="h-3.5 w-3.5" />
											</button>
										</div>
									</TableCell>
									<TableCell className="text-muted-foreground">
										{a.date}
									</TableCell>
									<TableCell>
										<div className="text-foreground flex items-center gap-2">
											<Icon className="text-muted-foreground h-4 w-4" />
											{a.attackType}
										</div>
									</TableCell>
									<TableCell className="pr-6 text-right">
										<Badge variant={SEVERITY_VARIANT[a.severity]}>
											{a.severity}
										</Badge>
									</TableCell>
								</TableRow>
							);
						})}
						{filtered.length === 0 && (
							<TableRow>
								<TableCell
									colSpan={4}
									className="text-muted-foreground text-center"
								>
									No alerts match the current filters.
								</TableCell>
							</TableRow>
						)}
					</TableBody>
				</Table>

				<footer className="border-border text-muted-foreground flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3 text-xs">
					<div className="flex items-center gap-2">
						<span>Show Rows</span>
						<Select defaultValue="10">
							<SelectTrigger className="h-7 w-[64px] text-xs">
								<SelectValue />
							</SelectTrigger>
							<SelectContent>
								<SelectItem value="10">10</SelectItem>
								<SelectItem value="25">25</SelectItem>
								<SelectItem value="50">50</SelectItem>
							</SelectContent>
						</Select>
					</div>
					<div>Total Risk Alerts Shown: {filtered.length}</div>
					<div className="flex items-center gap-1">
						<IconBtn aria-label="First page">
							<ChevronsLeft className="h-3.5 w-3.5" />
						</IconBtn>
						<IconBtn aria-label="Previous page">
							<ChevronLeft className="h-3.5 w-3.5" />
						</IconBtn>
						<span className="px-2">Page 1 of 500</span>
						<IconBtn aria-label="Next page">
							<ChevronRight className="h-3.5 w-3.5" />
						</IconBtn>
						<IconBtn aria-label="Last page">
							<ChevronsRight className="h-3.5 w-3.5" />
						</IconBtn>
					</div>
				</footer>
			</section>

			{/* Latest Transactions + Latest Blocks */}
			<div className="grid grid-cols-1 gap-4 md:grid-cols-2">
				<LatestList
					title="Latest Transactions"
					rows={latestTransactions.map((t) => ({
						primary: t.id,
						mono: true,
						middle: t.age,
						trailing: t.amountAda,
					}))}
				/>
				<LatestList
					title="Latest Blocks"
					rows={latestBlocks.map((b) => ({
						primary: b.height,
						mono: false,
						middle: b.age,
						trailing: b.amountAda,
					}))}
				/>
			</div>

			<div className="flex justify-end pt-2">
				<button
					type="button"
					onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
					className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1.5 text-xs"
				>
					<ArrowUp className="h-3.5 w-3.5" />
					Back to Top
				</button>
			</div>
		</div>
	);
}

function CriticalAlertCard() {
	return (
		<div className="border-severity-critical-foreground/40 bg-card ring-severity-critical/20 rounded-xl border p-4 ring-1 md:col-span-2">
			<div className="text-severity-critical-foreground flex items-center gap-2">
				<AlertTriangle className="h-4 w-4" />
				<span className="text-sm font-semibold">New Critical Attack</span>
			</div>
			<div className="text-muted-foreground mt-2 flex items-center gap-2 font-mono text-xs">
				<span className="truncate">{criticalAlertIdLong}</span>
				<button
					type="button"
					className="text-muted-foreground hover:text-foreground shrink-0"
					title="Copy"
				>
					<Copy className="h-3.5 w-3.5" />
				</button>
			</div>
		</div>
	);
}

function KpiCard({ label, value }: { label: string; value: string }) {
	return (
		<div className="border-border bg-card rounded-xl border p-4">
			<div className="text-foreground text-center text-sm font-semibold">
				{label}
			</div>
			<div className="text-brand mt-2 text-center text-2xl font-bold">
				{value}
			</div>
		</div>
	);
}

function GraphBarCard() {
	return (
		<div className="border-border bg-card rounded-xl border p-4">
			<div className="text-foreground text-sm font-semibold">Graph Bar</div>
			<Sparkline className="mt-2 h-10 w-full" />
		</div>
	);
}

function Sparkline({ className }: { className?: string }) {
	// Decorative SVG mimicking the figma mini-chart silhouette
	return (
		<svg
			className={className}
			viewBox="0 0 120 40"
			fill="none"
			xmlns="http://www.w3.org/2000/svg"
			preserveAspectRatio="none"
		>
			<path
				d="M0 32 L15 26 L25 30 L40 14 L55 28 L70 24 L85 8 L100 22 L120 18 L120 40 L0 40 Z"
				className="fill-brand/30"
			/>
			<path
				d="M0 32 L15 26 L25 30 L40 14 L55 28 L70 24 L85 8 L100 22 L120 18"
				className="stroke-brand"
				strokeWidth="1.5"
				fill="none"
			/>
		</svg>
	);
}

type ListRow = {
	primary: string;
	mono: boolean;
	middle: string;
	trailing: string;
};

function LatestList({ title, rows }: { title: string; rows: ListRow[] }) {
	return (
		<section className="border-border bg-card rounded-xl border">
			<header className="border-border border-b px-5 py-3">
				<h2 className="text-foreground text-base font-semibold">{title}</h2>
			</header>
			<ul className="divide-border/60 divide-y">
				{rows.map((r, i) => (
					<li
						key={i}
						className="grid grid-cols-3 items-center gap-2 px-5 py-3 text-sm"
					>
						<span
							className={cn(
								"text-foreground truncate",
								r.mono && "font-mono text-[13px]",
							)}
						>
							{r.primary}
						</span>
						<span className="text-muted-foreground text-center">
							{r.middle}
						</span>
						<span className="text-muted-foreground text-right">
							{r.trailing}
						</span>
					</li>
				))}
			</ul>
		</section>
	);
}

function IconBtn({
	children,
	...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
	return (
		<Button
			type="button"
			variant="ghost"
			size="icon"
			className="text-muted-foreground hover:text-foreground h-7 w-7"
			{...props}
		>
			{children}
		</Button>
	);
}
