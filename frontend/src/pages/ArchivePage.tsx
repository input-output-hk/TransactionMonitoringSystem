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
import { toast } from "sonner";
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
import { PageBtn } from "@/components/ui/page-button";
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
import { fetchArchiveForExport, type ArchiveEntry } from "@/lib/api/archive";
import { useArchivedAlerts } from "@/lib/archive-store";
import { ATTACK_ICON } from "@/lib/attack-display";
import { downloadCsv } from "@/lib/utils/csv";
import {
	formatAnalyzedAt,
	nDaysAgoISODate,
	nextDayISO,
	startOfDayISO,
	todayISODate,
} from "@/lib/utils/dates";
import { shortHash } from "@/lib/utils/strings";
import type { AttackType } from "@/mocks/attacks";

/** Confirm-over-N threshold for the export dialog, matches Reports. */
const EXPORT_CONFIRM_THRESHOLD = 1000;

export function ArchivePage() {
	const navigate = useNavigate();
	const allArchived = useArchivedAlerts();
	const [startDate, setStartDate] = useState(() => nDaysAgoISODate(60));
	const [endDate, setEndDate] = useState(todayISODate);
	const [exporting, setExporting] = useState(false);
	const [confirmExportOpen, setConfirmExportOpen] = useState(false);
	const [pageSize, setPageSize] = useState(10);
	const [page, setPage] = useState(0);

	// Visible list = archived entries within the selected date window.
	const visible = useMemo(() => {
		const fromMs = startOfDayMs(startDate);
		const toMs = nextDayMs(endDate);
		return allArchived.filter((e) => {
			const t = new Date(e.archived_at).getTime();
			return (fromMs == null || t >= fromMs) && (toMs == null || t < toMs);
		});
	}, [allArchived, startDate, endDate]);

	const total = visible.length;
	const pageCount = Math.max(1, Math.ceil(total / pageSize));
	const currentPage = Math.min(page, pageCount - 1);
	const pageRows = useMemo(
		() => visible.slice(currentPage * pageSize, (currentPage + 1) * pageSize),
		[visible, currentPage, pageSize],
	);

	/** Reset page index whenever a filter changes — wraps a date setter. */
	const onDateChange = (setter: (v: string) => void) => (v: string) => {
		setPage(0);
		setter(v);
	};

	const onExportClick = () => {
		if (exporting || visible.length === 0) return;
		if (visible.length > EXPORT_CONFIRM_THRESHOLD) {
			setConfirmExportOpen(true);
		} else {
			void runExport();
		}
	};

	const runExport = async () => {
		setConfirmExportOpen(false);
		setExporting(true);
		const dismiss = toast.loading("Preparing archive export…");
		try {
			const rows = await fetchArchiveForExport(
				{
					from: startOfDayISO(startDate),
					to: nextDayISO(endDate),
				},
				{
					onProgress: (fetched, total) => {
						toast.loading(
							`Preparing archive export… ${fetched.toLocaleString()} / ${total.toLocaleString()}`,
							{ id: dismiss },
						);
					},
				},
			);
			const stamp = todayISODate();
			downloadCsv(rows.map(toCsvRow), `archived-alerts-${stamp}.csv`);
			toast.success(`Exported ${rows.length.toLocaleString()} entries.`, {
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
			{/* Filter + actions bar */}
			<div className="flex flex-wrap items-end gap-3">
				<DateField
					id="archive-start"
					label="Start Date"
					value={startDate}
					onChange={onDateChange(setStartDate)}
				/>
				<DateField
					id="archive-end"
					label="End Date"
					value={endDate}
					onChange={onDateChange(setEndDate)}
				/>
				<div className="ml-auto pt-[22px]">
					<Button
						variant="outline"
						size="lg"
						className="h-11 gap-2"
						onClick={onExportClick}
						disabled={exporting || visible.length === 0}
					>
						{exporting ? "Exporting…" : "Export"}
						<ExternalLink className="h-4 w-4" />
					</Button>
				</div>
			</div>

			<section className="border-border bg-card rounded-lg border-2">
				<header className="border-border border-b px-5 py-3">
					<h2 className="text-foreground text-base font-semibold">
						Archived Attacks
					</h2>
				</header>

				<Table>
					<TableHeader>
						<TableRow className="hover:bg-transparent">
							<TableHead className="w-[28%]">ID</TableHead>
							<TableHead>Date</TableHead>
							<TableHead>Attack Type</TableHead>
							<TableHead>Reason</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{pageRows.map((a) => {
							const Icon =
								ATTACK_ICON[a.attack_type_snapshot as AttackType] ??
								AlertCircle;
							const displayId = shortHash(a.tx_hash);
							return (
								<TableRow
									key={a.tx_hash}
									onClick={() => navigate(`/archive/${a.tx_hash}`)}
									className="cursor-pointer"
								>
									<TableCell>
										<div className="text-foreground flex items-center gap-2 font-mono text-[13px]">
											<span>{displayId}</span>
											<button
												type="button"
												className="text-muted-foreground hover:text-foreground"
												title="Copy"
												onClick={(e) => {
													e.stopPropagation();
													navigator.clipboard?.writeText(a.tx_hash);
												}}
											>
												<Copy className="h-3.5 w-3.5" />
											</button>
										</div>
									</TableCell>
									<TableCell className="text-muted-foreground">
										{formatAnalyzedAt(a.archived_at)}
									</TableCell>
									<TableCell>
										<div className="text-foreground flex items-center gap-2">
											<Icon className="text-muted-foreground h-4 w-4" />
											{a.attack_type_snapshot}
										</div>
									</TableCell>
									<TableCell className="text-foreground">{a.reason}</TableCell>
								</TableRow>
							);
						})}
						{pageRows.length === 0 && (
							<TableRow>
								<TableCell
									colSpan={4}
									className="text-muted-foreground py-10 text-center"
								>
									{allArchived.length === 0
										? "No archived attacks yet."
										: "No archived attacks in the selected date range."}
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
					<div>Total Archived: {total.toLocaleString()}</div>
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

			{total > 0 && (
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
			)}

			<Dialog open={confirmExportOpen} onOpenChange={setConfirmExportOpen}>
				<DialogContent showClose={false} className="max-w-sm">
					<DialogHeader>
						<DialogTitle>
							Export {visible.length.toLocaleString()} entries?
						</DialogTitle>
						<DialogDescription>
							This is more than {EXPORT_CONFIRM_THRESHOLD.toLocaleString()}{" "}
							entries. The download may take a moment. Narrow the date range
							first if you don't need the full set.
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

/* ---------- helpers ---------- */

function startOfDayMs(date: string): number | null {
	if (!date) return null;
	return new Date(`${date}T00:00:00`).getTime();
}

function nextDayMs(date: string): number | null {
	if (!date) return null;
	const d = new Date(`${date}T00:00:00`);
	d.setDate(d.getDate() + 1);
	return d.getTime();
}

/** Flatten an ArchiveEntry into a CSV-friendly object. Column order matters. */
function toCsvRow(e: ArchiveEntry): Record<string, string | number> {
	return {
		tx_hash: e.tx_hash,
		archived_at: e.archived_at,
		reason: e.reason,
		notes: e.notes,
		archived_by: e.archived_by,
		attack_type_snapshot: e.attack_type_snapshot,
		severity_snapshot: e.severity_snapshot,
		risk_score_snapshot: e.risk_score_snapshot,
	};
}
