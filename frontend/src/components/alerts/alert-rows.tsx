/**
 * Row renderers for the contract-grouped risk-alerts table.
 *
 * The table mixes three row shapes in one body, all sharing the same column
 * grid so the header stays meaningful:
 *
 *  - a GROUP row: one contract, collapsed, with a chevron that expands its
 *    transactions inline;
 *  - a child row inside an expanded group;
 *  - a plain ALERT row for a transaction that names no contract, rendered
 *    ungrouped because there is nothing to put behind a chevron.
 *
 * The expand mechanics follow `components/clustering/ClusterSummaryTable`, which
 * already does row-expansion on these same table primitives: a shared column
 * count for colSpan bookkeeping, and `stopPropagation` on any nested control so
 * a copy button never also triggers the row's navigation.
 */
import { Badge } from "@/components/ui/badge";
import { TableCell, TableRow } from "@/components/ui/table";
import type { GroupedAlertRow } from "@/lib/api/analysis";
import { ATTACK_ICON, SEVERITY_VARIANT } from "@/lib/attack-display";
import type { RiskAlert } from "@/lib/attacks";
import { cn } from "@/lib/utils";
import { copyToClipboard } from "@/lib/utils/clipboard";
import { shortHash } from "@/lib/utils/strings";
import {
	AlertCircle,
	ChevronDown,
	ChevronRight,
	Copy,
	Pin,
} from "lucide-react";

/**
 * Columns in the alerts table body, including the leading chevron cell.
 * Every colSpan in this file derives from it so adding a column cannot leave a
 * mismatched expanded row behind.
 */
export const ALERT_COLUMN_COUNT = 5;

/** Truncation for a bech32 contract address shown as a group's fallback name. */
const CONTRACT_HEAD = 14;
const CONTRACT_TAIL = 6;

/**
 * Copies an identifier the table can only show truncated.
 *
 * Serves a transaction hash and a group's contract address alike: both are shown
 * shortened, and the address is often replaced outright by its registry label,
 * so neither can be selected off the screen.
 */
function CopyButton({ value, label }: { value: string; label: string }) {
	return (
		<button
			type="button"
			className="text-muted-foreground hover:text-foreground shrink-0"
			title="Copy"
			aria-label={label}
			onClick={(e) => {
				// The row navigates or expands on click; without this the copy also
				// triggers that.
				e.stopPropagation();
				// The FULL value, not the truncated display form.
				void copyToClipboard(value);
			}}
		>
			<Copy className="h-3.5 w-3.5" />
		</button>
	);
}

/**
 * Marks an auto-anomaly whose model could not cluster the contract.
 *
 * Shared by the alert row and the group row: the backend sets the marker on a
 * group from that group's WORST alert, so a group whose top row is an
 * un-clusterable verdict has to say so, or the operator gets the de-prioritise
 * signal only after expanding it.
 */
function UnclusterableBadge() {
	return (
		<Badge
			variant="outline"
			className="text-muted-foreground border-border/60 text-[9px] font-normal tracking-normal normal-case"
			title="This contract's model could not cluster its own data, so the anomaly is structural noise rather than a distinguishing signal. De-prioritized; it does not affect severity."
		>
			unclusterable model
		</Badge>
	);
}

function AttackTypeCell({ alert }: { alert: RiskAlert }) {
	const Icon = ATTACK_ICON[alert.attackType] ?? AlertCircle;
	return (
		<div className="text-foreground flex items-center gap-2">
			<Icon className="text-muted-foreground h-4 w-4" />
			{alert.attackType}
			{alert.unclusterableModel && <UnclusterableBadge />}
		</div>
	);
}

/**
 * A single transaction row: used for un-attributed alerts, for the children of
 * an expanded group, and for the pinned latest-critical row.
 */
export function AlertRow({
	alert,
	onOpen,
	indented = false,
	pinned = false,
}: {
	alert: RiskAlert;
	onOpen: (slug: string) => void;
	/** Nested inside an expanded group, so the chevron cell is a spacer. */
	indented?: boolean;
	/** The pinned latest-critical row. Rendered under PinnedCriticalHeaderRow. */
	pinned?: boolean;
}) {
	return (
		<TableRow
			onClick={() => onOpen(alert.slug)}
			className={cn(
				"cursor-pointer",
				// A tinted band closed by a faint critical edge, with the strong
				// accent scoped to the LEFT border only. Setting the border COLOUR
				// unscoped also recoloured the row's own bottom border, which read
				// as an accidental red underline instead of a deliberate block.
				pinned &&
					"bg-severity-critical/25 border-l-severity-critical-foreground border-b-severity-critical-foreground/30 border-l-2",
			)}
		>
			<TableCell className={cn("w-8", indented && "pl-6")} />
			<TableCell>
				<div className="text-foreground flex items-center gap-2 font-mono text-[13px] uppercase">
					<span>{alert.id}</span>
					<CopyButton value={alert.fullHash} label="Copy transaction hash" />
				</div>
			</TableCell>
			<TableCell className="text-foreground">{alert.date}</TableCell>
			<TableCell>
				<AttackTypeCell alert={alert} />
			</TableCell>
			<TableCell>
				<Badge variant={SEVERITY_VARIANT[alert.severity]}>
					{alert.severity}
				</Badge>
			</TableCell>
		</TableRow>
	);
}

/**
 * The attack type a group row names: the class of its WORST alert, which is the
 * same alert the severity badge on that line describes, plus a count of the
 * other kinds the group holds so a mixed contract is visible without expanding.
 */
function GroupAttackTypeCell({ row }: { row: GroupedAlertRow }) {
	// A deployment older than the field sends no class at all. The cell stays
	// empty rather than inventing a type for the row.
	if (!row.attackType) return null;
	const Icon = ATTACK_ICON[row.attackType] ?? AlertCircle;
	const others = row.attackTypes.filter((t) => t !== row.attackType);
	return (
		<div className="text-foreground flex items-center gap-2">
			<Icon className="text-muted-foreground h-4 w-4 shrink-0" />
			<span className="truncate">{row.attackType}</span>
			{others.length > 0 && (
				<span
					className="text-muted-foreground shrink-0 text-xs"
					title={`Also under this contract: ${others.join(", ")}`}
				>
					+{others.length} more
				</span>
			)}
			{row.unclusterableModel && <UnclusterableBadge />}
		</div>
	);
}

/**
 * A contract's collapsed alerts.
 *
 * `alertCount` is a server-side count under the active filters, not the number
 * of rows fetched, so it is safe to show as the group's total. It sits under the
 * contract name, where it describes the group; putting it in the Attack Type
 * column left that column promising a type and delivering a number.
 */
export function ContractGroupRow({
	row,
	label,
	expanded,
	onToggle,
}: {
	row: GroupedAlertRow;
	/** Registry display name when known, else a truncated address. */
	label?: string;
	expanded: boolean;
	onToggle: () => void;
}) {
	const Chevron = expanded ? ChevronDown : ChevronRight;
	return (
		<TableRow
			onClick={onToggle}
			className="cursor-pointer"
			aria-expanded={expanded}
		>
			<TableCell className="w-8">
				<Chevron className="text-muted-foreground h-4 w-4" />
			</TableCell>
			<TableCell>
				<div className="flex items-center gap-2">
					<span
						className={cn(
							"text-foreground truncate",
							!label && "font-mono text-[13px]",
						)}
						title={row.contractAddress}
					>
						{label ??
							shortHash(row.contractAddress, CONTRACT_HEAD, CONTRACT_TAIL)}
					</span>
					<CopyButton
						value={row.contractAddress}
						label="Copy contract address"
					/>
				</div>
				<div className="text-muted-foreground text-xs">
					{row.alertCount === 1
						? "1 alert"
						: `${row.alertCount.toLocaleString()} alerts`}
				</div>
			</TableCell>
			<TableCell className="text-foreground">{row.latestDate}</TableCell>
			<TableCell>
				<GroupAttackTypeCell row={row} />
			</TableCell>
			<TableCell>
				<Badge variant={SEVERITY_VARIANT[row.worstSeverity]}>
					{row.worstSeverity}
				</Badge>
			</TableCell>
		</TableRow>
	);
}

/**
 * Section label for the pinned latest-critical row.
 *
 * A strip ABOVE the row rather than a badge inside it: being pinned is a
 * property of the row's placement, so a badge sitting beside the hash reads as
 * an attribute of that transaction instead.
 */
export function PinnedCriticalHeaderRow() {
	return (
		<TableRow className="hover:bg-transparent">
			<TableCell
				colSpan={ALERT_COLUMN_COUNT}
				className="bg-severity-critical/25 border-t-severity-critical-foreground/30 text-severity-critical-foreground border-t py-1.5"
			>
				<div className="flex items-center gap-1.5 text-[10px] font-medium tracking-wider uppercase">
					<Pin className="h-3 w-3" />
					Latest critical
					<span className="text-muted-foreground font-normal tracking-normal normal-case">
						kept in view regardless of the sort below
					</span>
				</div>
			</TableCell>
		</TableRow>
	);
}

/** Air below the pinned block, so it and the sorted list read as two things. */
export function PinnedCriticalSpacerRow() {
	return (
		<TableRow className="hover:bg-transparent">
			<TableCell colSpan={ALERT_COLUMN_COUNT} className="h-3 p-0" />
		</TableRow>
	);
}

/** Full-width message row inside an expanded group (loading / error / empty). */
export function GroupMessageRow({ children }: { children: React.ReactNode }) {
	return (
		<TableRow className="hover:bg-transparent">
			<TableCell
				colSpan={ALERT_COLUMN_COUNT}
				className="text-muted-foreground py-4 pl-12 text-sm"
			>
				{children}
			</TableCell>
		</TableRow>
	);
}
