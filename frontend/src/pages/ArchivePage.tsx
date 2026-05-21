import { buttonVariants } from "@/components/ui/button-variants";
import { DateField } from "@/components/ui/date-field";
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
import { archiveApi } from "@/lib/api/archive";
import { useArchivedAlerts } from "@/lib/archive-store";
import { ATTACK_ICON } from "@/lib/attack-display";
import { cn } from "@/lib/utils";
import {
	formatAnalyzedAt,
	nDaysAgoISODate,
	nextDayISO,
	startOfDayISO,
	todayISODate,
} from "@/lib/utils/dates";
import { shortHash } from "@/lib/utils/strings";
import type { AttackType } from "@/mocks/attacks";
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
import { useState } from "react";
import { useNavigate } from "react-router-dom";

export function ArchivePage() {
	const navigate = useNavigate();
	const [startDate, setStartDate] = useState(() => nDaysAgoISODate(60));
	const [endDate, setEndDate] = useState(todayISODate);
	const [pageSize, setPageSize] = useState(10);
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

	// Server-streamed CSV export. Setting the anchor's `href` + clicking lets
	// the browser handle the download without us materializing the file in JS.
	const exportHref = archiveApi.exportUrl({
		from: startOfDayISO(startDate),
		to: nextDayISO(endDate),
	});

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
					<a
						href={exportHref}
						download
						className={cn(
							buttonVariants({ variant: "outline", size: "lg" }),
							"h-11 gap-2",
						)}
					>
						Export
						<ExternalLink className="h-4 w-4" />
					</a>
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
								(a.max_class && ATTACK_ICON[snakeToAttackType(a.max_class)]) ||
								AlertCircle;
							const displayId = shortHash(a.tx_hash);
							const displayClass = a.max_class
								? snakeToAttackType(a.max_class)
								: "—";
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
		</div>
	);
}

/* ---------- helpers ---------- */

/** Backend stores attack class in snake_case (`large_datum`); UI uses Title
 *  Case (`Large Datum`) for both display and the ATTACK_ICON map. */
function snakeToAttackType(snake: string): AttackType {
	return snake
		.split("_")
		.map((p) => p.charAt(0).toUpperCase() + p.slice(1))
		.join(" ") as AttackType;
}
