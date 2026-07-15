import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent } from "@/components/ui/dialog";
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
import { TableFooter } from "@/components/ui/table-footer";
import {
	Tooltip,
	TooltipContent,
	TooltipTrigger,
} from "@/components/ui/tooltip";
import { DEFAULT_PAGE_SIZE } from "@/lib/constants";
import { useRiskAlerts } from "@/lib/api/analysis";
import {
	useAlertTimeseries,
	useAnalysisStats,
	useTransactionThroughput,
} from "@/lib/api/stats";
import { useLatestTransactions, useRecentBlocks } from "@/lib/api/transactions";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import { cn } from "@/lib/utils";
import { copyToClipboard } from "@/lib/utils/clipboard";
import { formatTimeAgo } from "@/lib/utils/dates";
import { formatAdaCompact, PLACEHOLDER_KPI } from "@/lib/utils/numbers";
import { shortHash } from "@/lib/utils/strings";
import { ATTACK_TYPES, type AttackType, type Severity } from "@/lib/attacks";
import { AttackDetailPage } from "@/pages/AttackDetailPage";
import { AlertCircle, ArrowUp, Copy } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

// Recharts (+ its d3/redux transitive deps, ~280 KB gzip) loads as a
// deferred async chunk so it doesn't bloat the initial dashboard bundle.
const Sparkline = lazy(() => import("@/components/sparkline"));

export function AttacksPage() {
	const navigate = useNavigate();
	// `:id` is set only on `/attacks/:id` — same component renders the
	// dashboard at `/dashboard` (id undefined) and the dashboard + detail
	// popup at `/attacks/:id`.
	const { id: detailId } = useParams<{ id?: string }>();
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
	const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
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
	// Backend already anti-joins `archived_alerts` from `/api/v1/analysis/results`,
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
								{ value: "INFORMATIONAL", label: "Informational" },
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
							{/* Roughly equal-quarter columns (was ID=42% which crammed
							    the others). Severity is left-aligned to match Figma —
							    badge sits flush with the "Severity" header text. */}
							<TableHead className="w-[26%]">ID</TableHead>
							<TableHead className="w-[24%]">Date</TableHead>
							<TableHead className="w-[26%]">Attack Type</TableHead>
							<TableHead className="w-[24%]">Severity</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{visibleRows.map((a) => {
							const Icon = ATTACK_ICON[a.attackType] ?? AlertCircle;
							return (
								<TableRow
									key={a.slug}
									onClick={() => void navigate(`/attacks/${a.slug}`)}
									className="cursor-pointer"
								>
									<TableCell>
										<div className="text-foreground flex items-center gap-2 font-mono text-[13px] uppercase">
											<span>{a.id}</span>
											<button
												type="button"
												className="text-muted-foreground hover:text-foreground"
												title="Copy"
												onClick={(e) => {
													e.stopPropagation();
													// Copy the FULL hash, not the short display id — the
													// id is just the truncated render.
													void copyToClipboard(a.fullHash);
												}}
											>
												<Copy className="h-3.5 w-3.5" />
											</button>
										</div>
									</TableCell>
									<TableCell className="text-foreground">{a.date}</TableCell>
									<TableCell>
										<div className="text-foreground flex items-center gap-2">
											<Icon className="text-muted-foreground h-4 w-4" />
											{a.attackType}
										</div>
									</TableCell>
									<TableCell>
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

				<TableFooter
					pageSize={pageSize}
					onPageSizeChange={(n) => {
						setPageSize(n);
						setPage(0);
					}}
					centerLabel={`Total Risk Alerts Shown: ${visibleRows.length}`}
					page={currentPage}
					pageCount={pageCount}
					onPageChange={setPage}
				/>
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
						trailing: formatAdaCompact(t.total_output_value),
					}))}
				/>
				<LatestList
					title="Latest Blocks"
					isPending={recentBlocksPending}
					rows={(recentBlocks ?? []).map((b) => ({
						primary: String(b.block_height),
						mono: false,
						middle: formatTimeAgo(b.timestamp),
						trailing: formatAdaCompact(b.total_output_value),
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

			{/* Attack detail popup. Mounted on `/attacks/:id` while the dashboard
			    behind it stays alive (same component for both routes). Closing
			    the dialog (X, overlay click, ESC) navigates back to /dashboard,
			    so the URL stays in sync with what's visible. */}
			<Dialog
				open={!!detailId}
				onOpenChange={(open) => {
					if (!open) void navigate("/dashboard");
				}}
			>
				<DialogContent
					// Override the default small modal size + padding — we want
					// the detail card to drive its own layout edge-to-edge inside
					// the dialog frame, so strip the wrapper styling.
					className="max-h-[90vh] w-[min(90vw,1100px)] max-w-none overflow-y-auto border-none bg-transparent p-0 shadow-none"
					showClose={false}
				>
					{detailId && <AttackDetailPage />}
				</DialogContent>
			</Dialog>
		</div>
	);
}

/**
 * Latest CRITICAL alert banner. Pulls the most recent risk_band=Critical
 * row from `/api/v1/analysis/results` (sorted by date, page size 1) and shares
 * the table's 15s poll cadence — `useRiskAlerts` uses its `params` as the
 * query key, so a separate page=0/pageSize=1 request lives independently.
 *
 * Three visual states:
 *  - Loading: muted placeholder, no critical styling yet.
 *  - Found: full critical theme, clickable, copy button.
 *  - Empty (no critical alerts at all): neutral border so the red doesn't lie.
 */
/** Inline icon matching the Figma "critical" banner: red filled triangle
 *  with a white exclamation. Drawn here to avoid the lucide AlertTriangle
 *  stroke-only look. */
function CriticalTriangleIcon({ className }: { className?: string }) {
	return (
		<svg
			className={className}
			viewBox="0 0 24 24"
			fill="none"
			xmlns="http://www.w3.org/2000/svg"
			aria-hidden="true"
		>
			<path
				d="M10.95 3.06a1.2 1.2 0 0 1 2.1 0l9.45 16.74A1.2 1.2 0 0 1 21.45 21.6H2.55a1.2 1.2 0 0 1-1.05-1.8z"
				fill="#dc2626"
			/>
			<path d="M12 9v5" stroke="white" strokeWidth="2" strokeLinecap="round" />
			<circle cx="12" cy="17.2" r="1.05" fill="white" />
		</svg>
	);
}

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
		"bg-card border-border flex flex-col justify-center rounded-lg border-2 p-4 md:col-span-2 transition-colors";
	const interactiveCls = latest ? "cursor-pointer hover:bg-accent/50" : "";

	return (
		<div
			className={cn(baseCls, interactiveCls)}
			onClick={latest ? () => void navigate(`/attacks/${latest.slug}`) : undefined}
			role={latest ? "button" : undefined}
			tabIndex={latest ? 0 : undefined}
			onKeyDown={
				latest
					? (e) => {
							if (e.key === "Enter" || e.key === " ") {
								e.preventDefault();
								void navigate(`/attacks/${latest.slug}`);
							}
						}
					: undefined
			}
		>
			<div className="text-foreground flex items-center justify-center gap-2">
				<CriticalTriangleIcon className="h-5 w-5 shrink-0" />
				<span className="text-lg font-semibold">
					{latest
						? "New Critical Attack"
						: isPending
							? "Critical Attacks"
							: "No Critical Attacks"}
				</span>
			</div>
			<div className="text-foreground mt-2 flex items-center justify-center gap-2 font-mono text-xs">
				<span className="truncate">
					{latest
						? shortHash(latest.fullHash.toUpperCase(), 19, 11)
						: isPending
							? "Loading…"
							: "—"}
				</span>
				{latest && (
					<button
						type="button"
						className="text-muted-foreground hover:text-foreground shrink-0"
						title="Copy"
						onClick={(e) => {
							e.stopPropagation();
							void copyToClipboard(latest.fullHash);
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
		<div className="border-border bg-card flex flex-col justify-center rounded-lg border-2 p-4">
			<div className="text-foreground text-center text-lg font-semibold">
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
		<div className="border-border bg-card flex flex-col justify-center rounded-lg border-2 p-4">
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
		<section className="border-border bg-background rounded-lg border-2">
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
								// IDs (tx_hash) are mono + uppercase to match the Risk
								// Alerts table styling. Block heights are plain numbers
								// so the mono flag flips both off.
								r.mono && "font-mono text-[13px] uppercase",
							)}
						>
							{r.primary}
						</span>
						<span className="text-foreground text-center">{r.middle}</span>
						<span className="text-foreground text-right">{r.trailing}</span>
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
