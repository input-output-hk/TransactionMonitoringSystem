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
	FileWarning,
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

function CopyHashButton({ hash }: { hash: string }) {
	return (
		<button
			type="button"
			className="text-muted-foreground hover:text-foreground"
			title="Copy"
			aria-label="Copy transaction hash"
			onClick={(e) => {
				// The row navigates on click; without this the copy also opens the
				// detail view.
				e.stopPropagation();
				// The FULL hash, not the truncated display id.
				void copyToClipboard(hash);
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
	/** The pinned latest-critical row, marked so it reads as deliberate. */
	pinned?: boolean;
}) {
	return (
		<TableRow
			onClick={() => onOpen(alert.slug)}
			className={cn(
				"cursor-pointer",
				// Left accent in the critical token so the pinned row is legible as
				// pinned context rather than as a sort anomaly.
				pinned && "border-severity-critical-foreground/60 border-l-2",
			)}
		>
			<TableCell className={cn("w-8", indented && "pl-6")} />
			<TableCell>
				<div className="text-foreground flex items-center gap-2 font-mono text-[13px] uppercase">
					<span>{alert.id}</span>
					<CopyHashButton hash={alert.fullHash} />
					{pinned && (
						<Badge
							variant="outline"
							className="text-muted-foreground border-border/60 text-[9px] font-normal tracking-normal normal-case"
							title="The most recent Critical alert, kept in view regardless of the sort order below."
						>
							latest critical
						</Badge>
					)}
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
 * A contract's collapsed alerts. `alertCount` is a server-side count under the
 * active filters, not the number of rows fetched, so it is shown as the total.
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
					<FileWarning className="text-muted-foreground h-4 w-4 shrink-0" />
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
				</div>
			</TableCell>
			<TableCell className="text-foreground">{row.latestDate}</TableCell>
			<TableCell className="text-muted-foreground">
				<div className="flex items-center gap-2">
					{row.alertCount === 1 ? "1 alert" : `${row.alertCount} alerts`}
					{row.unclusterableModel && <UnclusterableBadge />}
				</div>
			</TableCell>
			<TableCell>
				<Badge variant={SEVERITY_VARIANT[row.worstSeverity]}>
					{row.worstSeverity}
				</Badge>
			</TableCell>
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
