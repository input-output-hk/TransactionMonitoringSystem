import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
	AlertCircle,
	ArrowUp,
	ChevronLeft,
	ChevronRight,
	ChevronsLeft,
	ChevronsRight,
	Copy,
	ExternalLink,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DateField } from "@/components/ui/date-field";
import { PageBtn } from "@/components/ui/page-button";
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
import { ATTACK_TYPES, type AttackType, type Severity } from "@/mocks/attacks";
import { fetchAlertsForExport, useRiskAlerts } from "@/lib/api/analysis";
import { useArchiveSnapshot } from "@/lib/archive-store";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import { cn } from "@/lib/utils";
import { downloadCsv } from "@/lib/utils/csv";
import {
	defaultEnd,
	defaultStart,
	nextDayISO,
	startOfDayISO,
} from "@/lib/utils/dates";
import { toast } from "sonner";

export function ReportsPage() {
	const navigate = useNavigate();
	const archivedSlugs = useArchiveSnapshot();
	const [startDate, setStartDate] = useState(defaultStart());
	const [endDate, setEndDate] = useState(defaultEnd());
	const [attackFilter, setAttackFilter] = useState<string>("all");
	const [severityFilter, setSeverityFilter] = useState<string>("all");
	const [sortBy, setSortBy] = useState<"date" | "score">("date");
	const [pageSize, setPageSize] = useState(10);
	const [page, setPage] = useState(0);
	const [exporting, setExporting] = useState(false);
	const [confirmExportOpen, setConfirmExportOpen] = useState(false);

	const { data, isPending, isError } = useRiskAlerts(
		{
			page,
			pageSize,
			attackType:
				attackFilter !== "all" ? (attackFilter as AttackType) : undefined,
			severity:
				severityFilter !== "all" ? (severityFilter as Severity) : undefined,
			analyzedFrom: startOfDayISO(startDate),
			analyzedTo: nextDayISO(endDate),
			sort: sortBy,
		},
		// Reports is a deliberate-query view; no need to auto-poll.
		{ pollMs: 0 },
	);

	const total = data?.total ?? 0;
	const visibleRows = useMemo(() => {
		const archivedSet = new Set(archivedSlugs);
		return (data?.rows ?? []).filter((a) => !archivedSet.has(a.slug));
	}, [data, archivedSlugs]);

	const pageCount = Math.max(1, Math.ceil(total / pageSize));
	const currentPage = Math.min(page, pageCount - 1);

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
					severity:
						severityFilter !== "all" ? (severityFilter as Severity) : undefined,
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

	const resetPageOnChange = <T,>(setter: (v: T) => void) => {
		return (v: T) => {
			setPage(0);
			setter(v);
		};
	};

	return (
		<div className="flex flex-col gap-4">
			{/* Filter bar */}
			<div className="flex flex-wrap items-end gap-3">
				<DateField
					id="report-start"
					label="Start Date"
					value={startDate}
					onChange={resetPageOnChange(setStartDate)}
				/>
				<DateField
					id="report-end"
					label="End Date"
					value={endDate}
					onChange={resetPageOnChange(setEndDate)}
				/>

				<div className="flex flex-col gap-1.5">
					<Label htmlFor="report-attack" className="text-foreground text-xs">
						Attack Type
					</Label>
					<Select
						value={attackFilter}
						onValueChange={resetPageOnChange(setAttackFilter)}
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
						onValueChange={resetPageOnChange(setSeverityFilter)}
					>
						<SelectTrigger id="report-severity" className="h-11 w-[200px]">
							<SelectValue placeholder="All" />
						</SelectTrigger>
						<SelectContent>
							<SelectItem value="all">All</SelectItem>
							<SelectItem value="LOW">Low</SelectItem>
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
						onValueChange={(v) => {
							setPage(0);
							setSortBy(v as "date" | "score");
						}}
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
								<TableCell
									colSpan={4}
									className="text-status-offline py-10 text-center"
								>
									Failed to load risk alerts.
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
					<div>Total Risk Alerts: {total.toLocaleString()}</div>
					<div className="flex items-center gap-1">
						<PageBtn
							aria-label="First page"
							disabled={currentPage === 0}
							onClick={() => setPage(0)}
						>
							<ChevronsLeft className="h-3.5 w-3.5" />
						</PageBtn>
						<PageBtn
							aria-label="Previous page"
							disabled={currentPage === 0}
							onClick={() => setPage((p) => Math.max(0, p - 1))}
						>
							<ChevronLeft className="h-3.5 w-3.5" />
						</PageBtn>
						<span className="px-2">
							Page {currentPage + 1} of {pageCount}
						</span>
						<PageBtn
							aria-label="Next page"
							disabled={currentPage >= pageCount - 1}
							onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
						>
							<ChevronRight className="h-3.5 w-3.5" />
						</PageBtn>
						<PageBtn
							aria-label="Last page"
							disabled={currentPage >= pageCount - 1}
							onClick={() => setPage(pageCount - 1)}
						>
							<ChevronsRight className="h-3.5 w-3.5" />
						</PageBtn>
					</div>
				</footer>
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
							onClick={runExport}
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
