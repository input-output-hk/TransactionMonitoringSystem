import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DateField } from "@/components/ui/date-field";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
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
import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS } from "@/lib/constants";
import { TableFooter } from "@/components/ui/table-footer";
import { fetchAlertsForExport, useRiskAlerts } from "@/lib/api/analysis";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import { qpDate, qpEnum, useQueryParamState } from "@/lib/url-state";
import { copyToClipboard } from "@/lib/utils/clipboard";
import { downloadCsv } from "@/lib/utils/csv";
import {
	defaultEnd,
	defaultStart,
	nextDayISO,
	startOfDayISO,
} from "@/lib/utils/dates";
import { ATTACK_TYPES, type AttackType, type Severity } from "@/lib/attacks";
import { AlertCircle, ArrowUp, Copy, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

export function ReportsPage() {
	const navigate = useNavigate();
	// The URL is the single source of truth for filters, sort, page and page
	// size, so any filtered view can be shared or bookmarked (helpers shared
	// with ValidatorDetailPage via lib/url-state). Defaults are omitted from
	// the URL to keep shared links short.
	const { searchParams, setParam } = useQueryParamState();
	const startDate = qpDate(searchParams, "from", defaultStart());
	const endDate = qpDate(searchParams, "to", defaultEnd());
	const attackFilter = qpEnum<string>(
		searchParams,
		"attack",
		["all", ...ATTACK_TYPES],
		"all",
	);
	const severityFilter = qpEnum<string>(
		searchParams,
		"severity",
		["all", "INFORMATIONAL", "MEDIUM", "HIGH", "CRITICAL"],
		"all",
	);
	const sortBy = qpEnum(searchParams, "sort", ["date", "score"] as const, "date");
	// The URL carries 1-based page numbers ("page=2" is the second page);
	// everything below stays 0-based to match TableFooter. parseInt yields
	// NaN for garbage, which fails the >= 1 test and falls back to page 0.
	const qpPage = Number.parseInt(searchParams.get("page") ?? "1", 10);
	const page = qpPage >= 1 ? qpPage - 1 : 0;
	// Page size rides in the URL too: a shared ?page=N is meaningless unless
	// the recipient also gets the size that defines the offset. Only values
	// the picker offers are accepted.
	const qpSize = Number.parseInt(searchParams.get("size") ?? "", 10);
	const pageSize = PAGE_SIZE_OPTIONS.includes(qpSize)
		? qpSize
		: DEFAULT_PAGE_SIZE;
	const [exporting, setExporting] = useState(false);
	const [confirmExportOpen, setConfirmExportOpen] = useState(false);

	// Any filter/sort/size change drops `page`: the old offset is
	// meaningless against a different result set.
	const setFilterParam = (key: string, value: string | null) =>
		setParam(key, value, { alsoDelete: ["page"] });

	// ReportsPage keeps the single-select UX but the API now expects an
	// array of severities, so we wrap the lone selection.
	const severities: Severity[] | undefined =
		severityFilter !== "all" ? [severityFilter as Severity] : undefined;

	const { data, isPending, isError, refetch } = useRiskAlerts(
		{
			page,
			pageSize,
			attackType:
				attackFilter !== "all" ? (attackFilter as AttackType) : undefined,
			severities,
			analyzedFrom: startOfDayISO(startDate),
			analyzedTo: nextDayISO(endDate),
			sort: sortBy,
		},
		// Reports is a deliberate-query view; no need to auto-poll.
		{ pollMs: 0 },
	);

	const total = data?.total ?? 0;
	// Backend already anti-joins `archived_alerts` from `/api/v1/analysis/results`.
	const visibleRows = data?.rows ?? [];

	const pageCount = Math.max(1, Math.ceil(total / pageSize));
	const currentPage = Math.min(page, pageCount - 1);

	// Self-heal an out-of-range ?page= (stale shared link, or the result set
	// shrank): once the total is known, snap the URL to the last real page so
	// the table and its pager agree instead of stranding the view on an empty
	// slice with a misleading "no alerts match" message.
	useEffect(() => {
		if (isPending || isError || data === undefined) return;
		if (page > pageCount - 1) {
			setParam("page", pageCount > 1 ? String(pageCount) : null);
		}
	}, [isPending, isError, data, page, pageCount, setParam]);

	/** Ask for confirmation before exporting more than this many rows. */
	const EXPORT_CONFIRM_THRESHOLD = 1000;

	const onExportClick = () => {
		if (exporting || total === 0) return;
		if (total > EXPORT_CONFIRM_THRESHOLD) {
			setConfirmExportOpen(true);
		} else {
			void runExport();
		}
	};

	const runExport = async () => {
		setConfirmExportOpen(false);
		setExporting(true);
		const dismiss = toast.loading("Preparing export…");
		try {
			const rows = await fetchAlertsForExport(
				{
					attackType:
						attackFilter !== "all" ? (attackFilter as AttackType) : undefined,
					severities,
					analyzedFrom: startOfDayISO(startDate),
					analyzedTo: nextDayISO(endDate),
					sort: sortBy,
				},
				{
					onProgress: (fetched, total) => {
						toast.loading(
							`Preparing export… ${fetched.toLocaleString()} / ${total.toLocaleString()}`,
							{ id: dismiss },
						);
					},
				},
			);
			const stamp = new Date().toISOString().slice(0, 10);
			downloadCsv(rows, `risk-alerts-${stamp}.csv`);
			toast.success(`Exported ${rows.length.toLocaleString()} alerts.`, {
				id: dismiss,
			});
		} catch (e) {
			console.error(e);
			toast.error("Export failed. Please try again.", { id: dismiss });
		} finally {
			setExporting(false);
		}
	};

	return (
		<div className="flex flex-col gap-4">
			{/* Filter bar */}
			<div className="flex flex-wrap items-end gap-3">
				<DateField
					id="report-start"
					label="Start Date"
					value={startDate}
					onChange={(v) => setFilterParam("from", v)}
				/>
				<DateField
					id="report-end"
					label="End Date"
					value={endDate}
					onChange={(v) => setFilterParam("to", v)}
				/>

				<div className="flex flex-col gap-1.5">
					<Label htmlFor="report-attack" className="text-foreground text-xs">
						Attack Type
					</Label>
					<Select
						value={attackFilter}
						onValueChange={(v) => setFilterParam("attack", v === "all" ? null : v)}
					>
						<SelectTrigger id="report-attack" className="h-11 w-[200px]">
							<SelectValue placeholder="All" />
						</SelectTrigger>
						<SelectContent>
							<SelectItem value="all">All</SelectItem>
							{ATTACK_TYPES.map((t) => (
								<SelectItem key={t} value={t}>
									{t}
								</SelectItem>
							))}
						</SelectContent>
					</Select>
				</div>

				<div className="flex flex-col gap-1.5">
					<Label htmlFor="report-severity" className="text-foreground text-xs">
						Severity Type
					</Label>
					<Select
						value={severityFilter}
						onValueChange={(v) => setFilterParam("severity", v === "all" ? null : v)}
					>
						<SelectTrigger id="report-severity" className="h-11 w-[200px]">
							<SelectValue placeholder="All" />
						</SelectTrigger>
						<SelectContent>
							<SelectItem value="all">All</SelectItem>
							<SelectItem value="INFORMATIONAL">Informational</SelectItem>
							<SelectItem value="MEDIUM">Medium</SelectItem>
							<SelectItem value="HIGH">High</SelectItem>
							<SelectItem value="CRITICAL">Critical</SelectItem>
						</SelectContent>
					</Select>
				</div>

				<div className="flex flex-col gap-1.5">
					<Label htmlFor="report-sort" className="text-foreground text-xs">
						Sort By
					</Label>
					<Select
						value={sortBy}
						onValueChange={(v) => setFilterParam("sort", v === "date" ? null : v)}
					>
						<SelectTrigger id="report-sort" className="h-11 w-[180px]">
							<SelectValue />
						</SelectTrigger>
						<SelectContent>
							<SelectItem value="date">Most recent</SelectItem>
							<SelectItem value="score">Highest risk</SelectItem>
						</SelectContent>
					</Select>
				</div>

				<div className="ml-auto pt-[22px]">
					<Button
						variant="outline"
						size="lg"
						className="h-11 gap-2"
						onClick={onExportClick}
						disabled={exporting || total === 0}
					>
						{exporting ? "Exporting…" : "Export"}
						<ExternalLink className="h-4 w-4" />
					</Button>
				</div>
			</div>

			{/* Risk Alerts */}
			<section className="border-border bg-card rounded-lg border-2">
				<header className="border-border border-b px-5 py-3">
					<h2 className="text-foreground text-base font-semibold">
						Risk Alerts
					</h2>
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
													void copyToClipboard(a.fullHash);
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
						{!isPending && !isError && visibleRows.length === 0 && (
							<TableRow>
								<TableCell
									colSpan={4}
									className="text-muted-foreground py-10 text-center"
								>
									No risk alerts match the current filters.
								</TableCell>
							</TableRow>
						)}
						{isPending && (
							<TableRow>
								<TableCell
									colSpan={4}
									className="text-muted-foreground py-10 text-center"
								>
									Loading…
								</TableCell>
							</TableRow>
						)}
						{isError && (
							<TableRow>
								<TableCell colSpan={4} className="py-10 text-center">
									<div className="flex flex-col items-center gap-3">
										<span className="text-status-offline">
											Failed to load risk alerts.
										</span>
										<Button
											variant="outline"
											size="sm"
											onClick={() => void refetch()}
										>
											Retry
										</Button>
									</div>
								</TableCell>
							</TableRow>
						)}
					</TableBody>
				</Table>

				<TableFooter
					pageSize={pageSize}
					onPageSizeChange={(n) =>
						setFilterParam("size", n === DEFAULT_PAGE_SIZE ? null : String(n))
					}
					centerLabel={`Total Risk Alerts: ${total.toLocaleString()}`}
					page={currentPage}
					pageCount={pageCount}
					onPageChange={(p) => setParam("page", p <= 0 ? null : String(p + 1))}
				/>
			</section>

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

			<Dialog open={confirmExportOpen} onOpenChange={setConfirmExportOpen}>
				<DialogContent showClose={false} className="max-w-sm">
					<DialogHeader>
						<DialogTitle>Export {total.toLocaleString()} rows?</DialogTitle>
						<DialogDescription>
							This is more than {EXPORT_CONFIRM_THRESHOLD.toLocaleString()}{" "}
							rows. The download may take a moment. Narrow the filters first if
							you don't need the full set.
						</DialogDescription>
					</DialogHeader>
					<DialogFooter>
						<Button
							variant="outline"
							onClick={() => setConfirmExportOpen(false)}
						>
							Cancel
						</Button>
						<Button
							onClick={() => void runExport()}
							className="border-border text-brand hover:bg-accent hover:text-brand border bg-transparent"
						>
							Confirm
						</Button>
					</DialogFooter>
				</DialogContent>
			</Dialog>
		</div>
	);
}
