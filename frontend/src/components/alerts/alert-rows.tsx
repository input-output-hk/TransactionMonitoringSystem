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
 * Tint for the pinned latest-critical row, and `border-b-0` so no divider splits
 * the block. The accent is PINNED_ACCENT, on the first cell rather than here.
 */
const PINNED_ROW = "bg-severity-critical/25 border-b-0";

/**
 * The left accent, applied to the row's FIRST CELL.
 *
 * Tailwind's preflight sets `border-collapse: collapse` on every table, which
 * means a border declared on a `<tr>` is resolved against the borders of the
 * cells, the row group and the column before it paints. A cell's left border in
 * the first column has nothing to be resolved against, so it always paints the
 * full cell height: the accent cannot come up short or land off the row edge.
 */
const PINNED_ACCENT = "border-l-severity-critical-foreground border-l-2";

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

/**
 * Marks the row the table holds at the top.
 *
 * Deliberately NOT a badge. As an outlined box it was wide enough to wrap onto
 * two lines inside the ID cell, which made a tall red rectangle beside a
 * single-line hash: nothing about it lined up with anything. As plain text it
 * shares the hash's line box, so it is aligned by construction and `nowrap`
 * keeps it that way.
 *
 * One word, because the severity badge at the other end of the same row already
 * reads CRITICAL; repeating it there bought width and no information. Which
 * critical alert this is belongs in the title, not in the row.
 *
 * Placed AFTER the hash so every row's ID still starts on the same column: an
 * indented hash on one row reads as a broken table.
 */
function PinnedCriticalMarker() {
	return (
		<span
			className="text-severity-critical-foreground flex shrink-0 items-center gap-1 text-[10px] font-semibold tracking-wider whitespace-nowrap uppercase"
			title="The most recent Critical alert, pinned above the list."
		>
			<Pin className="h-2.5 w-2.5" />
			Pinned
		</span>
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
	/** The pinned latest-critical row: tinted, accented, and marked as pinned. */
	pinned?: boolean;
}) {
	return (
		<TableRow
			onClick={() => onOpen(alert.slug)}
			className={cn(
				"cursor-pointer",
				// The hover tint has to be restated: TableRow ships
				// `hover:bg-muted/40`, and tailwind-merge does not drop it for a
				// plain `bg-*`, so without this the row turns grey the moment the
				// analyst points at it and stops reading as pinned.
				pinned && `${PINNED_ROW} hover:bg-severity-critical/30`,
			)}
		>
			<TableCell
				className={cn("w-8", indented && "pl-6", pinned && PINNED_ACCENT)}
			/>
			<TableCell>
				<div className="flex items-center gap-2">
					{/* Mono and uppercase belong to the hash, not to the cell: the marker
					    beside it is prose and would inherit both. */}
					<span className="text-foreground font-mono text-[13px] uppercase">
						{alert.id}
					</span>
					<CopyButton value={alert.fullHash} label="Copy transaction hash" />
					{pinned && <PinnedCriticalMarker />}
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
	const Icon = row.attackType
		? (ATTACK_ICON[row.attackType] ?? AlertCircle)
		: null;
	const others = row.attackTypes.filter((t) => t !== row.attackType);
	return (
		<div className="text-foreground flex items-center gap-2">
			{/* A deployment older than the field sends no class, and the cell then
			    names no type rather than inventing one. The un-clusterable marker
			    is deliberately OUTSIDE that condition: "do not trust this model"
			    is the signal that must survive longest, and tying it to a field it
			    does not depend on would be a trap for whoever changes this next. */}
			{Icon && row.attackType && (
				<>
					<Icon className="text-muted-foreground h-4 w-4 shrink-0" />
					<span className="truncate">{row.attackType}</span>
					{others.length > 0 && (
						<span
							className="text-muted-foreground shrink-0 text-xs"
							// Scoped to the filter, like every other number on the row:
							// `classes` comes from the same filtered GROUP BY, so under an
							// attack-class filter a mixed contract honestly shows none.
							title={`Also under this contract, matching the current filter: ${others.join(", ")}`}
						>
							+{others.length} more
						</span>
					)}
				</>
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
 * Air below the pinned row, so it and the sorted list read as two things.
 *
 * `border-b-0` matters: TableRow ships `border-b`, so this row drew a divider
 * 12px under the pinned block, belonging to nothing and reading as a line out of
 * alignment with the block above it.
 */
export function PinnedCriticalSpacerRow() {
	return (
		<TableRow className="border-b-0 hover:bg-transparent">
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
