/**
 * Grouped run picker shared by the clustering and anomaly control bars. Runs are
 * split by origin: the canonical System run(s) that drive scoring are pinned at
 * the top, the user's Custom experiments sit below a divider. Each option shows a
 * type-specific summary (via `getLabel`) plus a timestamp so near-identical runs
 * stay distinguishable. Generic over the run shape so both `Run` and `AnomalyRun`
 * (which share origin/created_at) can use it.
 */
import type { RunOrigin } from "@/lib/api/clustering";
import {
	Select,
	SelectContent,
	SelectGroup,
	SelectItem,
	SelectLabel,
	SelectSeparator,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";

type SelectableRun = {
	run_id: string;
	origin: RunOrigin;
	created_at: string;
};

/** Compact local-time label for a ClickHouse timestamp ("YYYY-MM-DD HH:MM:SS…").
 *  Treated as UTC (the sidecar stores/logs UTC); falls back to the raw string if
 *  it can't be parsed. */
function fmtRunTime(created_at: string): string {
	const iso = created_at.replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(created_at) ? "" : "Z");
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return created_at;
	return d.toLocaleString(undefined, {
		month: "short",
		day: "numeric",
		hour: "2-digit",
		minute: "2-digit",
	});
}

export function RunSelect<T extends SelectableRun>({
	runs,
	value,
	onChange,
	getLabel,
	triggerClassName = "h-9 w-[22rem] max-w-full",
}: {
	runs: T[];
	value: string;
	onChange: (runId: string) => void;
	getLabel: (run: T) => string;
	triggerClassName?: string;
}) {
	const system = runs.filter((r) => r.origin === "system");
	const custom = runs.filter((r) => r.origin === "custom");

	const option = (r: T) => (
		<SelectItem key={r.run_id} value={r.run_id}>
			{getLabel(r)} · {fmtRunTime(r.created_at)}
		</SelectItem>
	);

	return (
		<Select value={value} onValueChange={onChange} disabled={!runs.length}>
			<SelectTrigger className={triggerClassName}>
				<SelectValue placeholder="No runs yet" />
			</SelectTrigger>
			<SelectContent>
				{system.length > 0 && (
					<SelectGroup>
						<SelectLabel>Canonical · drives scoring</SelectLabel>
						{system.map(option)}
					</SelectGroup>
				)}
				{system.length > 0 && custom.length > 0 && <SelectSeparator />}
				{custom.length > 0 && (
					<SelectGroup>
						<SelectLabel>Your experiments</SelectLabel>
						{custom.map(option)}
					</SelectGroup>
				)}
			</SelectContent>
		</Select>
	);
}
