import { buttonVariants } from "@/components/ui/button-variants";
import { DateField } from "@/components/ui/date-field";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import { TableFooter } from "@/components/ui/table-footer";
import { DEFAULT_PAGE_SIZE } from "@/lib/constants";
import { archiveApi } from "@/lib/api/archive";
import { useArchivedAlerts } from "@/lib/archive-store";
import { ATTACK_ICON } from "@/lib/attack-display";
import { cn } from "@/lib/utils";
import { copyToClipboard } from "@/lib/utils/clipboard";
import {
	formatAnalyzedAt,
	nDaysAgoISODate,
	nextDayISO,
	startOfDayISO,
	todayISODate,
} from "@/lib/utils/dates";
import { shortHash } from "@/lib/utils/strings";
import { attackTypeFromSnake } from "@/lib/api/analysis";
import { AttackDetailPage } from "@/pages/AttackDetailPage";
import { AlertCircle, ArrowUp, Copy, ExternalLink } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

export function ArchivePage() {
	const navigate = useNavigate();
	// `:id` is set only on `/archive/:id` — same component renders the
	// archive table at `/archive` (id undefined) and the table + detail
	// popup at `/archive/:id`, mirroring the dashboard overlay.
	const { id: detailId } = useParams<{ id?: string }>();
	const [startDate, setStartDate] = useState(() => nDaysAgoISODate(60));
	const [endDate, setEndDate] = useState(todayISODate);
	const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
	const [page, setPage] = useState(0);

	const {
		data: rows,
		total,
		isPending,
		isError,
	} = useArchivedAlerts({
		from: startOfDayISO(startDate),
		to: nextDayISO(endDate),
		limit: pageSize,
		offset: page * pageSize,
	});

	const pageCount = Math.max(1, Math.ceil(total / pageSize));
	const currentPage = Math.min(page, pageCount - 1);

	/** Reset page index whenever a filter changes — wraps a date setter. */
	const onDateChange = (setter: (v: string) => void) => (v: string) => {
		setPage(0);
		setter(v);
	};

	const [exporting, setExporting] = useState(false);

	const handleExport = async () => {
		if (exporting) return;
		setExporting(true);
		try {
			const { blob, filename } = await archiveApi.download({
				from: startOfDayISO(startDate),
				to: nextDayISO(endDate),
			});
			const url = URL.createObjectURL(blob);
			const a = document.createElement("a");
			a.href = url;
			a.download = filename;
			document.body.appendChild(a);
			a.click();
			a.remove();
			URL.revokeObjectURL(url);
		} catch (e) {
			console.error("Archive export failed:", e);
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
					<button
						type="button"
						onClick={handleExport}
						disabled={exporting}
						className={cn(
							buttonVariants({ variant: "outline", size: "lg" }),
							"h-11 gap-2",
						)}
					>
						{exporting ? "Exporting…" : "Export"}
						<ExternalLink className="h-4 w-4" />
					</button>
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
						{rows.map((a) => {
							const Icon =
								(a.max_class && ATTACK_ICON[attackTypeFromSnake(a.max_class)]) ||
								AlertCircle;
							const displayId = shortHash(a.tx_hash);
							const displayClass = a.max_class
								? attackTypeFromSnake(a.max_class)
								: "—";
							return (
								<TableRow
									key={a.tx_hash}
									onClick={() => navigate(`/archive/${a.tx_hash}`)}
									className="cursor-pointer"
								>
									<TableCell>
										<div className="text-foreground flex items-center gap-2 font-mono text-[13px] uppercase">
											<span>{displayId}</span>
											<button
												type="button"
												className="text-muted-foreground hover:text-foreground"
												title="Copy"
												onClick={(e) => {
													e.stopPropagation();
													copyToClipboard(a.tx_hash);
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
											{displayClass}
										</div>
									</TableCell>
									<TableCell className="text-foreground">{a.note}</TableCell>
								</TableRow>
							);
						})}
						{!isPending && !isError && rows.length === 0 && (
							<TableRow>
								<TableCell
									colSpan={4}
									className="text-muted-foreground py-10 text-center"
								>
									{total === 0
										? "No archived attacks yet."
										: "No archived attacks in the selected date range."}
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
									Failed to load archive.
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
					centerLabel={`Total Archived: ${total.toLocaleString()}`}
					page={currentPage}
					pageCount={pageCount}
					onPageChange={setPage}
				/>
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

			<Dialog
				open={!!detailId}
				onOpenChange={(open) => {
					if (!open) navigate("/archive");
				}}
			>
				<DialogContent
					className="max-h-[90vh] w-[min(90vw,1100px)] max-w-none overflow-y-auto border-none bg-transparent p-0 shadow-none"
					showClose={false}
				>
					{detailId && <AttackDetailPage archived />}
				</DialogContent>
			</Dialog>
		</div>
	);
}

