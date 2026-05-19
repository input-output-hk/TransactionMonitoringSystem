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
import { useRiskAlerts } from "@/lib/api/analysis";
import { useArchiveSnapshot } from "@/lib/archive-store";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import { cn } from "@/lib/utils";
import {
	defaultEnd,
	defaultStart,
	nextDayISO,
	startOfDayISO,
} from "@/lib/utils/dates";

export function ReportsPage() {
	const navigate = useNavigate();
	const archivedSlugs = useArchiveSnapshot();
	const [startDate, setStartDate] = useState(defaultStart());
	const [endDate, setEndDate] = useState(defaultEnd());
	const [attackFilter, setAttackFilter] = useState<string>("all");
	const [severityFilter, setSeverityFilter] = useState<string>("all");
	const [pageSize, setPageSize] = useState(10);
	const [page, setPage] = useState(0);

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
			sort: "date",
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

				<div className="ml-auto pt-[22px]">
					<Button variant="outline" size="lg" className="h-11 gap-2" disabled>
						Export
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
		</div>
	);
}

function DateField({
	id,
	label,
	value,
	onChange,
}: {
	id: string;
	label: string;
	value: string;
	onChange: (v: string) => void;
}) {
	return (
		<div className="flex flex-col gap-1.5">
			<Label htmlFor={id} className="text-foreground text-xs">
				{label}
			</Label>
			<input
				id={id}
				type="date"
				value={value}
				onChange={(e) => onChange(e.target.value)}
				className={cn(
					"border-border bg-input/40 text-foreground flex h-11 w-[180px] items-center rounded-sm border px-3 py-2 text-sm transition-colors",
					"focus-visible:ring-ring focus-visible:ring-offset-background focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:outline-none",
				)}
			/>
		</div>
	);
}

function PageBtn({
	children,
	onClick,
	disabled,
	...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
	return (
		<button
			type="button"
			onClick={onClick}
			disabled={disabled}
			className={cn(
				"inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors",
				"hover:bg-accent hover:text-foreground",
				"disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent",
			)}
			{...props}
		>
			{children}
		</button>
	);
}
