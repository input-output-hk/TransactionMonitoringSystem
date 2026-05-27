import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { MultiSelect } from "@/components/ui/multi-select";
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
	Tooltip,
	TooltipContent,
	TooltipTrigger,
} from "@/components/ui/tooltip";
import { useRiskAlerts } from "@/lib/api/analysis";
import {
	useAlertTimeseries,
	useAnalysisStats,
	useTransactionStats,
	useTransactionThroughput,
} from "@/lib/api/stats";
import { useLatestTransactions, useRecentBlocks } from "@/lib/api/transactions";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import { cn } from "@/lib/utils";
import { formatTimeAgo } from "@/lib/utils/dates";
import { formatAda, PLACEHOLDER_KPI } from "@/lib/utils/numbers";
import { shortHash } from "@/lib/utils/strings";
import { ATTACK_TYPES, type AttackType, type Severity } from "@/mocks/attacks";
import {
	AlertCircle,
	AlertTriangle,
	ArrowUp,
	ChevronLeft,
	ChevronRight,
	ChevronsLeft,
	ChevronsRight,
	Copy,
} from "lucide-react";
import { lazy, Suspense, useState } from "react";
import { useNavigate } from "react-router-dom";

// Recharts (+ its d3/redux transitive deps, ~280 KB gzip) loads as a
// deferred async chunk so it doesn't bloat the initial dashboard bundle.
const Sparkline = lazy(() => import("@/components/sparkline"));

export function AttacksPage() {
	const navigate = useNavigate();
	const [attackFilter, setAttackFilter] = useState<string>("all");
	// Multi-select: empty array means "no severity filter applied".
	// Default to High + Critical so the dashboard opens focused on the
	// actionable alerts. Order matches the MultiSelect option order
	// (LOW → MEDIUM → HIGH → CRITICAL) so the first user toggle doesn't
	// cause a no-op reorder → cache miss in React Query.
	const [severities, setSeverities] = useState<Severity[]>([
		"HIGH",
		"CRITICAL",
	]);
	const [pageSize, setPageSize] = useState(10);
	const [page, setPage] = useState(0);

	const { data, isPending, isError, error } = useRiskAlerts({
		page,
		pageSize,
		attackType:
			attackFilter !== "all" ? (attackFilter as AttackType) : undefined,
		// Skip the param entirely when nothing is picked so the backend
		// doesn't see an empty `?risk_band=` and apply a no-op filter.
		severities: severities.length > 0 ? severities : undefined,
	});

	const total = data?.total ?? 0;
	// Backend already anti-joins `archived_alerts` from `/api/analysis/results`,
	// so the rows we get are guaranteed not archived. No client filter needed.
	const visibleRows = data?.rows ?? [];

	const pageCount = Math.max(1, Math.ceil(total / pageSize));
	const currentPage = Math.min(page, pageCount - 1);

	const onAttackChange = (value: string) => {
		setPage(0);
		setAttackFilter(value);
	};
	const onSeveritiesChange = (next: Severity[]) => {
		setPage(0);
		setSeverities(next);
	};

	// Live KPI cards
	const { data: analysisStats } = useAnalysisStats();
	const { data: txStats } = useTransactionStats();
	// 5-minute sliding window — matches the backend default and the
	// 15s poll cadence keeps the value reactive without spamming.
	const { data: throughput } = useTransactionThroughput(5);
	const { data: latestTxs, isPending: latestTxsPending } =
		useLatestTransactions(5);
	const { data: recentBlocks, isPending: recentBlocksPending } =
		useRecentBlocks(5);

	const kpis = [
		{
			label: "TX / min",
			value: throughput
				? Math.round(throughput.tx_per_min).toLocaleString()
				: PLACEHOLDER_KPI,
		},
		{
			label: "Pending",
			value: analysisStats
				? analysisStats.pending_count.toLocaleString()
				: PLACEHOLDER_KPI,
		},
		{
			label: "Critical",
			value: analysisStats
				? analysisStats.critical_count.toLocaleString()
				: PLACEHOLDER_KPI,
		},
		{
			label: "Avg Risk",
			value:
				analysisStats && analysisStats.avg_max_score !== null
					? analysisStats.avg_max_score.toFixed(1)
					: PLACEHOLDER_KPI,
		},
	];

	return (
		<div className="flex flex-col gap-4">
			{/* Top KPI row */}
			<div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-7">
				<CriticalAlertCard />
				{kpis.map((k) => (
					<KpiCard key={k.label} label={k.label} value={k.value} />
				))}
				<GraphBarCard />
			</div>

			{/* Risk Alerts */}
			<section className="border-border bg-card rounded-lg border-2">
				<header className="border-border flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3">
					<h2 className="text-foreground text-base font-semibold">
						Risk Alerts
					</h2>
					<div className="flex items-center gap-2">
						<Select value={attackFilter} onValueChange={onAttackChange}>
							<SelectTrigger className="h-8 w-40">
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
						<MultiSelect<Severity>
							options={[
								{ value: "LOW", label: "Low" },
								{ value: "MEDIUM", label: "Medium" },
								{ value: "HIGH", label: "High" },
								{ value: "CRITICAL", label: "Critical" },
							]}
							value={severities}
							onChange={onSeveritiesChange}
							placeholder="All severities"
							label="severity"
							pluralLabel="severities"
						/>
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
						{visibleRows.map((a) => {
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
						{visibleRows.length === 0 && (
							<TableRow>
								<TableCell
									colSpan={4}
									className="text-muted-foreground py-8 text-center"
								>
									{isPending
										? "Loading risk alerts…"
										: isError
											? `Failed to load: ${error instanceof Error ? error.message : "unknown error"}`
											: "No alerts match the current filters."}
								</TableCell>
							</TableRow>
						)}
					</TableBody>
				</Table>

				<footer className="border-border text-muted-foreground flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3 text-xs">
					<div className="flex items-center gap-2">
						<span>Show Rows</span>
						<Select
							value={String(pageSize)}
							onValueChange={(v) => {
								setPageSize(Number(v));
								setPage(0);
							}}
						>
							<SelectTrigger className="h-7 w-16 text-xs">
								<SelectValue />
							</SelectTrigger>
							<SelectContent>
								<SelectItem value="10">10</SelectItem>
								<SelectItem value="25">25</SelectItem>
								<SelectItem value="50">50</SelectItem>
							</SelectContent>
						</Select>
					</div>
					<div>Total Risk Alerts: {total.toLocaleString()}</div>
					<div className="flex items-center gap-1">
						<IconBtn
							aria-label="First page"
							disabled={currentPage === 0}
							onClick={() => setPage(0)}
						>
							<ChevronsLeft className="h-3.5 w-3.5" />
						</IconBtn>
						<IconBtn
							aria-label="Previous page"
							disabled={currentPage === 0}
							onClick={() => setPage((p) => Math.max(0, p - 1))}
						>
							<ChevronLeft className="h-3.5 w-3.5" />
						</IconBtn>
						<span className="px-2">
							Page {currentPage + 1} of {pageCount}
						</span>
						<IconBtn
							aria-label="Next page"
							disabled={currentPage >= pageCount - 1}
							onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
						>
							<ChevronRight className="h-3.5 w-3.5" />
						</IconBtn>
						<IconBtn
							aria-label="Last page"
							disabled={currentPage >= pageCount - 1}
							onClick={() => setPage(pageCount - 1)}
						>
							<ChevronsRight className="h-3.5 w-3.5" />
						</IconBtn>
					</div>
				</footer>
			</section>

			{/* Latest Transactions + Latest Blocks */}
			<div className="grid grid-cols-1 gap-4 md:grid-cols-2">
				<LatestList
					title="Latest Transactions"
					isPending={latestTxsPending}
					rows={(latestTxs ?? []).map((t) => ({
						primary: shortHash(t.tx_hash),
						mono: true,
						middle: formatTimeAgo(t.timestamp),
						trailing: formatAda(t.total_output_value),
					}))}
				/>
				<LatestList
					title="Latest Blocks"
					isPending={recentBlocksPending}
					rows={(recentBlocks ?? []).map((b) => ({
						primary: String(b.block_height),
						mono: false,
						middle: formatTimeAgo(b.timestamp),
						trailing: formatAda(b.total_output_value),
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

/**
 * Latest CRITICAL alert banner. Pulls the most recent risk_band=Critical
 * row from `/api/analysis/results` (sorted by date, page size 1) and shares
 * the table's 5s poll cadence — `useRiskAlerts` uses its `params` as the
 * query key, so a separate page=0/pageSize=1 request lives independently.
 *
 * Three visual states:
 *  - Loading: muted placeholder, no critical styling yet.
 *  - Found: full critical theme, clickable, copy button.
 *  - Empty (no critical alerts at all): neutral border so the red doesn't lie.
 */
function CriticalAlertCard() {
	const navigate = useNavigate();
	const { data, isPending } = useRiskAlerts({
		page: 0,
		pageSize: 1,
		severities: ["CRITICAL"],
		sort: "date",
	});
	const latest = data?.rows[0];

	const baseCls =
		"bg-card rounded-lg border-2 p-4 md:col-span-2 transition-colors";
	const themedCls = latest
		? "border-severity-critical-foreground/40 ring-severity-critical/20 ring-1 cursor-pointer hover:bg-accent/50"
		: "border-border";

	return (
		<div
			className={cn(baseCls, themedCls)}
			onClick={latest ? () => navigate(`/attacks/${latest.slug}`) : undefined}
			role={latest ? "button" : undefined}
			tabIndex={latest ? 0 : undefined}
			onKeyDown={
				latest
					? (e) => {
							if (e.key === "Enter" || e.key === " ") {
								e.preventDefault();
								navigate(`/attacks/${latest.slug}`);
							}
						}
					: undefined
			}
		>
			<div
				className={cn(
					"flex items-center gap-2",
					latest
						? "text-severity-critical-foreground"
						: "text-muted-foreground",
				)}
			>
				<AlertTriangle className="h-4 w-4" />
				<span className="text-sm font-semibold">
					{latest
						? "New Critical Attack"
						: isPending
							? "Critical Attacks"
							: "No Critical Attacks"}
				</span>
			</div>
			<div className="text-muted-foreground mt-2 flex items-center gap-2 font-mono text-xs">
				<span className="truncate">
					{latest?.fullHash ?? (isPending ? "Loading…" : "—")}
				</span>
				{latest && (
					<button
						type="button"
						className="text-muted-foreground hover:text-foreground shrink-0"
						title="Copy"
						onClick={(e) => {
							e.stopPropagation();
							navigator.clipboard?.writeText(latest.fullHash);
						}}
					>
						<Copy className="h-3.5 w-3.5" />
					</button>
				)}
			</div>
		</div>
	);
}

function KpiCard({ label, value }: { label: string; value: string }) {
	return (
		<div className="border-border bg-card rounded-lg border-2 p-4">
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
	// High+Critical alerts/day over the last 14 days, bucketed on on-chain
	// block time (see backend get_alert_timeseries). Gives the Critical KPI
	// a trend so operators can tell a spike from the baseline.
	const { data, isPending, isError } = useAlertTimeseries(14);
	const points = data?.data ?? [];
	const total = points.reduce((sum, p) => sum + p.count, 0);

	return (
		<div className="border-border bg-card rounded-lg border-2 p-4">
			<div className="flex items-baseline justify-between">
				<Tooltip>
					<TooltipTrigger asChild>
						<div className="text-foreground cursor-help text-sm font-semibold underline decoration-dotted underline-offset-4">
							Severe Alerts
						</div>
					</TooltipTrigger>
					<TooltipContent side="top" className="max-w-xs text-xs">
						Daily count of Critical + High severity alerts over the last 14 days
						(by on-chain block time).
					</TooltipContent>
				</Tooltip>
				<div className="text-muted-foreground text-xs">14d</div>
			</div>
			{isPending || isError ? (
				<div className="text-muted-foreground mt-2 flex h-10 items-center text-xs">
					{isError ? "Unavailable" : "Loading…"}
				</div>
			) : (
				<Suspense
					fallback={
						<div className="text-muted-foreground mt-2 flex h-10 items-center text-xs">
							Loading…
						</div>
					}
				>
					<Sparkline points={points} className="mt-2 h-10 w-full" />
				</Suspense>
			)}
			<div className="text-muted-foreground mt-1 text-xs">
				{total} in last 14 days
			</div>
		</div>
	);
}

type ListRow = {
	primary: string;
	mono: boolean;
	middle: string;
	trailing: string;
};

function LatestList({
	title,
	rows,
	isPending,
}: {
	title: string;
	rows: ListRow[];
	isPending?: boolean;
}) {
	return (
		<section className="border-border bg-card rounded-lg border-2">
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
				{rows.length === 0 && (
					<li className="text-muted-foreground px-5 py-6 text-center text-sm">
						{isPending ? "Loading…" : "No data yet."}
					</li>
				)}
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
