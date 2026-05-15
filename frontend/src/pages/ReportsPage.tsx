import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlertCircle, ArrowUp, Copy, ExternalLink } from "lucide-react";
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
import { ATTACK_TYPES } from "@/mocks/attacks";
import { useActiveAlerts } from "@/lib/archive-store";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import { cn } from "@/lib/utils";

// "DD.MM.YYYY, HH:mm" → Date
function parseAlertDate(s: string): Date {
	const [datePart, timePart = "00:00"] = s.split(", ");
	const [dd, mm, yyyy] = datePart.split(".");
	const [hh, min] = timePart.split(":");
	return new Date(
		Number(yyyy),
		Number(mm) - 1,
		Number(dd),
		Number(hh),
		Number(min),
	);
}

export function ReportsPage() {
	const navigate = useNavigate();
	const activeAlerts = useActiveAlerts();
	const [startDate, setStartDate] = useState("2026-02-01");
	const [endDate, setEndDate] = useState("2026-03-01");
	const [attackFilter, setAttackFilter] = useState<string>("all");
	const [severityFilter, setSeverityFilter] = useState<string>("all");

	const filtered = useMemo(() => {
		const from = startDate ? new Date(startDate) : null;
		const to = endDate ? new Date(endDate) : null;
		if (to) to.setHours(23, 59, 59, 999);
		return activeAlerts.filter((a) => {
			if (attackFilter !== "all" && a.attackType !== attackFilter) return false;
			if (severityFilter !== "all" && a.severity !== severityFilter)
				return false;
			const d = parseAlertDate(a.date);
			if (from && d < from) return false;
			if (to && d > to) return false;
			return true;
		});
	}, [activeAlerts, attackFilter, severityFilter, startDate, endDate]);

	return (
		<div className="flex flex-col gap-4">
			{/* Filter bar */}
			<div className="flex flex-wrap items-end gap-3">
				<DateField
					id="report-start"
					label="Start Date"
					value={startDate}
					onChange={setStartDate}
				/>
				<DateField
					id="report-end"
					label="End Date"
					value={endDate}
					onChange={setEndDate}
				/>

				<div className="flex flex-col gap-1.5">
					<Label htmlFor="report-attack" className="text-foreground text-xs">
						Attack Type
					</Label>
					<Select value={attackFilter} onValueChange={setAttackFilter}>
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
					<Select value={severityFilter} onValueChange={setSeverityFilter}>
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
					<Button variant="outline" size="lg" className="h-11 gap-2">
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
									No risk alerts match the current filters.
								</TableCell>
							</TableRow>
						)}
					</TableBody>
				</Table>
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
					"border-border bg-input/40 text-foreground flex h-11 w-[180px] items-center rounded-md border px-3 py-2 text-sm transition-colors",
					"focus-visible:ring-ring focus-visible:ring-offset-background focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:outline-none",
				)}
			/>
		</div>
	);
}
