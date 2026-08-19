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
import {
	ALERT_COLUMN_COUNT,
	AlertRow,
	ContractGroupRow,
	PinnedCriticalSpacerRow,
} from "@/components/alerts/alert-rows";
import { ContractGroupAlerts } from "@/components/alerts/contract-group-alerts";
import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS } from "@/lib/constants";
import {
	useGroupedAlerts,
	useRiskAlerts,
	type GroupedAlertRow,
} from "@/lib/api/analysis";
import { useContracts } from "@/lib/api/clustering";
import { useHealth } from "@/lib/api/health";
import { qpEnum, useQueryParamState } from "@/lib/url-state";
import {
	useAlertTimeseries,
	useAnalysisStats,
	useTransactionThroughput,
} from "@/lib/api/stats";
import { useLatestTransactions, useRecentBlocks } from "@/lib/api/transactions";
import { cn } from "@/lib/utils";
import { formatTimeAgo } from "@/lib/utils/dates";
import { formatAdaCompact, PLACEHOLDER_KPI } from "@/lib/utils/numbers";
import { shortHash } from "@/lib/utils/strings";
import {
	ATTACK_TYPES,
	avgRiskHelp,
	type RiskAlert,
	type Severity,
} from "@/lib/attacks";
import { AttackDetailPage } from "@/pages/AttackDetailPage";
import { ArrowUp, Info } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Sparkline from "@/components/sparkline";

const SEVERITY_VALUES: readonly Severity[] = [
	"INFORMATIONAL",
	"MODERATE",
	"HIGH",
	"CRITICAL",
];

/** Default severity filter: open the dashboard on the actionable alerts. */
const DEFAULT_SEVERITIES: readonly Severity[] = ["HIGH", "CRITICAL"];

/**
 * Parse the `?severity=` CSV, preserving the canonical option order.
 *
 * Order matters beyond cosmetics: the parsed array is part of the React Query
 * key, so a differently-ordered but equivalent selection would miss the cache.
 * An absent param means "use the default"; an explicitly empty one means "no
 * severity filter", which is why the two are not collapsed.
 */
function parseSeverities(raw: string | null): Severity[] {
	if (raw === null) return [...DEFAULT_SEVERITIES];
	const picked = new Set(raw.split(",").filter(Boolean));
	return SEVERITY_VALUES.filter((s) => picked.has(s));
}

/**
 * Poll cadence for the contract registry, which supplies the group rows' display
 * names. Slower than the alert polling on purpose: the registry only changes when
 * an operator adds or renames a watched contract, so the hook's 10s default would
 * put a steady request on the sidecar for data that is effectively static.
 */
const CONTRACT_LABEL_POLL_MS = 60_000;

/** Validate `?size=` against the sizes the picker actually offers. */
function parsePageSize(raw: string | null): number {
	const n = Number(raw);
	return PAGE_SIZE_OPTIONS.includes(n) ? n : DEFAULT_PAGE_SIZE;
}

/**
 * Project an un-attributed grouped row onto the shape the shared row renderer
 * takes. Returns null when the row is missing the transaction identity it needs
 * to be clickable, so a malformed row is skipped rather than rendered as a dead
 * row.
 */
function groupedRowToAlert(row: GroupedAlertRow): RiskAlert | null {
	if (!row.txHash || !row.attackType) return null;
	return {
		slug: row.txHash,
		id: shortHash(row.txHash),
		fullHash: row.txHash,
		date: row.latestDate,
		attackType: row.attackType,
		severity: row.worstSeverity,
		riskScore: row.worstScore,
		// Not carried by the grouped aggregate; the detail view fetches them.
		feeAda: 0,
		outputs: 0,
		unclusterableModel: row.unclusterableModel ?? false,
		contractAddress: "",
	};
}

export function AttacksPage() {
	const navigate = useNavigate();
	// `:id` is set only on `/attacks/:id` — same component renders the
	// dashboard at `/dashboard` (id undefined) and the dashboard + detail
	// popup at `/attacks/:id`.
	const { id: detailId } = useParams<{ id?: string }>();
	// Filter and page state lives in the URL (same discipline as ReportsPage):
	// grouping makes a view worth sharing and worth surviving a reload, and this
	// was the only list page still holding it in component state.
	const { searchParams, setParam } = useQueryParamState();
	const attackFilter = qpEnum(
		searchParams,
		"attack",
		["all", ...ATTACK_TYPES],
		"all",
	);
	// Empty array means "no severity filter applied". Defaults to High + Critical
	// so the dashboard opens focused on the actionable alerts. Order matches the
	// MultiSelect option order so the first user toggle doesn't cause a no-op
	// reorder and a React Query cache miss.
	const severities = parseSeverities(searchParams.get("severity"));
	const pageSize = parsePageSize(searchParams.get("size"));
	// 1-based in the URL, 0-based in state.
	const page = Math.max(0, (Number(searchParams.get("page")) || 1) - 1);
	// Which contract group is expanded. Single-open, matching ClusterSummaryTable.
	const [expandedContract, setExpandedContract] = useState<string | null>(null);

	const filters = {
		// qpEnum already narrows to "all" | AttackType, so no cast is needed.
		attackType: attackFilter !== "all" ? attackFilter : undefined,
		// Skip the param entirely when nothing is picked so the backend
		// doesn't see an empty `?risk_band=` and apply a no-op filter.
		severities: severities.length > 0 ? severities : undefined,
	};

	const { data, isPending, isError, error } = useGroupedAlerts({
		...filters,
		page,
		pageSize,
	});

	// The pinned latest-critical row.
	//
	// Filter-aware in both directions: it inherits the attack-type filter, and it
	// exists at all only while the severity selection admits Critical. Pinning a
	// Critical row above a Moderate-only table would contradict the filter the
	// operator set, so the query is held off rather than merely hidden.
	//
	// FIRST PAGE ONLY. This page offers no sort control: the list is ordered by
	// date server-side, so the pin's whole job is keeping the latest Critical at
	// the top of the list even when the newest alerts are not Critical. Page 2
	// onwards has no top of the list, and a July Critical pinned above August
	// alerts belongs to neither that page nor the operator's intent.
	const criticalSelected =
		severities.length === 0 || severities.includes("CRITICAL");
	const onFirstPage = page === 0;
	const pinEnabled = criticalSelected && onFirstPage;
	const { data: criticalData } = useRiskAlerts(
		{
			...filters,
			severities: ["CRITICAL"],
			page: 0,
			pageSize: 1,
			sort: "date",
		},
		{ enabled: pinEnabled },
	);
	const pinnedCritical = pinEnabled ? criticalData?.rows[0] : undefined;

	// Human contract names come from the clustering registry. Gated on the health
	// flag so a clustering-disabled deployment never polls the sidecar; without it
	// a group falls back to its truncated address.
	const health = useHealth();
	const clusteringEnabled = health.data?.clustering_enabled === true;
	const { data: contracts } = useContracts(
		CONTRACT_LABEL_POLL_MS,
		clusteringEnabled,
	);
	const contractLabels = useMemo(() => {
		const byAddress = new Map<string, string>();
		for (const c of contracts ?? []) {
			if (c.label) byAddress.set(c.target, c.label);
		}
		return byAddress;
	}, [contracts]);

	// Rows for the pager, alerts for the label. The grouped endpoint reports both
	// because they differ: one group row can stand for dozens of alerts, so the
	// row total cannot answer "how many alerts". Asking the flat list instead
	// would be neither free (a 1-row page still runs the contract_anomaly recall
	// rescue) nor right (that rescue's date floor collapses to the single row it
	// returned, so the count would omit the anomaly alerts this table shows).
	const total = data?.total ?? 0;
	const totalAlerts = data?.alertTotal ?? 0;
	// Backend already anti-joins `archived_alerts` from `/api/v1/analysis/results`,
	// so the rows we get are guaranteed not archived. No client filter needed.
	const visibleRows = data?.rows ?? [];

	const pageCount = Math.max(1, Math.ceil(total / pageSize));
	const currentPage = Math.min(page, pageCount - 1);
	// Rows that will actually render. An un-attributed row missing its tx identity
	// is skipped, so keying the empty state off visibleRows.length could leave the
	// table with no rows AND no message.
	const renderableRows = visibleRows.filter(
		(r) => r.kind === "group" || groupedRowToAlert(r) !== null,
	);

	// A filter change invalidates the page number and any open group.
	const setFilterParam = (key: string, value: string | null) => {
		setExpandedContract(null);
		setParam(key, value, { alsoDelete: ["page"] });
	};
	const onAttackChange = (value: string) => {
		setFilterParam("attack", value === "all" ? null : value);
	};
	const onSeveritiesChange = (next: Severity[]) => {
		setFilterParam("severity", next.length ? next.join(",") : null);
	};
	const onPageChange = (next: number) => {
		setExpandedContract(null);
		setParam("page", next === 0 ? null : String(next + 1));
	};
	// Carry the query string across the detail route. The filters live in the URL
	// now, and `/attacks/:id` renders this same page under the dialog, so
	// navigating without them would reset the table behind the popup and leave the
	// operator back at the defaults when they close it.
	const search = searchParams.toString();
	const openDetail = (slug: string) =>
		void navigate({ pathname: `/attacks/${slug}`, search });

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
			// The client reported this number as unclear. The copy lives with the
			// other operator-facing strings in lib/attacks.ts.
			help: avgRiskHelp(analysisStats?.avg_max_score),
		},
	];

	return (
		<div className="flex flex-col gap-4">
			{/* Top KPI row */}
			<div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-5">
				{kpis.map((k) => (
					<KpiCard
						key={k.label}
						label={k.label}
						value={k.value}
						help={k.help}
					/>
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
								{ value: "MODERATE", label: "Moderate" },
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
							{/* Leading spacer for the group chevron. Severity is
							    left-aligned to match Figma — badge sits flush with the
							    "Severity" header text. */}
							<TableHead className="w-8" />
							<TableHead className="w-[26%]">Contract / ID</TableHead>
							<TableHead className="w-[22%]">Date</TableHead>
							<TableHead className="w-[26%]">Attack Type</TableHead>
							<TableHead className="w-[22%]">Severity</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{/* Pinned first, and de-duplicated against the body so the same
						    transaction never appears twice. One row: the tint, the accent and
						    the marker inside the row say it is pinned, so nothing here adds a
						    second row the operator would read as another alert. */}
						{pinnedCritical && (
							<Fragment key={`pinned-${pinnedCritical.slug}`}>
								<AlertRow
									alert={pinnedCritical}
									onOpen={openDetail}
									pinned
									contractLabel={contractLabels.get(
										pinnedCritical.contractAddress ?? "",
									)}
								/>
								<PinnedCriticalSpacerRow />
							</Fragment>
						)}
						{visibleRows.map((row) => {
							if (row.kind === "alert") {
								// Names no contract, so it renders ungrouped rather than
								// behind a chevron with nothing to label it.
								const alert = groupedRowToAlert(row);
								if (!alert || alert.slug === pinnedCritical?.slug) return null;
								return (
									<AlertRow
										key={alert.slug}
										alert={alert}
										onOpen={openDetail}
									/>
								);
							}
							const expanded = expandedContract === row.contractAddress;
							return (
								<Fragment key={`group-${row.contractAddress}`}>
									<ContractGroupRow
										row={row}
										label={contractLabels.get(row.contractAddress)}
										expanded={expanded}
										onToggle={() =>
											setExpandedContract(expanded ? null : row.contractAddress)
										}
									/>
									{expanded && (
										<ContractGroupAlerts
											contract={row.contractAddress}
											alertCount={row.alertCount}
											filters={filters}
											onOpen={openDetail}
										/>
									)}
								</Fragment>
							);
						})}
						{renderableRows.length === 0 && !pinnedCritical && (
							<TableRow>
								<TableCell
									colSpan={ALERT_COLUMN_COUNT}
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
						setExpandedContract(null);
						setParam("size", n === DEFAULT_PAGE_SIZE ? null : String(n), {
							alsoDelete: ["page"],
						});
					}}
					centerLabel={`Total Risk Alerts: ${totalAlerts.toLocaleString()}`}
					page={currentPage}
					pageCount={pageCount}
					onPageChange={onPageChange}
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
					if (!open) void navigate({ pathname: "/dashboard", search });
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

function KpiCard({
	label,
	value,
	help,
}: {
	label: string;
	value: string;
	/** Explanatory text behind an info icon. Same pattern as DonutCard. */
	help?: string;
}) {
	return (
		<div className="border-border bg-card relative flex flex-col justify-center rounded-lg border-2 p-4">
			{help && (
				<Tooltip>
					<TooltipTrigger asChild>
						<button
							type="button"
							aria-label={`What ${label} means`}
							className="text-muted-foreground/70 hover:text-foreground focus-visible:ring-ring absolute top-3 right-3 rounded focus-visible:ring-2 focus-visible:outline-none"
						>
							<Info className="h-3.5 w-3.5" />
						</button>
					</TooltipTrigger>
					<TooltipContent side="top" align="end" className="max-w-xs text-xs">
						{help}
					</TooltipContent>
				</Tooltip>
			)}
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
				<Sparkline points={points} className="mt-2 h-10 w-full" />
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
