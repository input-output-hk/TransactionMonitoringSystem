/**
 * Top anomaly candidates for a run, ranked by ensemble consensus. Rows flagged
 * by ≥2 detectors are emphasised; the per-detector scores (iso / lof / dbscan)
 * are the evidence, the verdict is the judgement, and an analyst can override a
 * single tx with the inline label control. Reskinned to the TMS design system.
 */
import { useState } from "react";

import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import { TableFooter } from "@/components/ui/table-footer";
import { EmptyText, ErrorText, LoadingText } from "@/components/ui/status-text";
import { type AnomalyCandidate, useTopAnomalies } from "@/lib/api/clustering";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { CopyHash, VerdictBadge } from "./cells";
import { WEEKDAYS, formatInt } from "./format";
import { formatAdaExact } from "@/lib/utils/numbers";
import { ReasonChips } from "./ReasonChips";
import { TxLabelControl } from "./TxLabelControl";

// Votes at or above this many independent detectors is the strong signal worth
// highlighting (each detector flags only its most extreme ~5%).
const STRONG_VOTE_THRESHOLD = 2;

type Props = { runId: string; target: string };

export function AnomalyTable({ runId, target }: Props) {
	const { isAdmin } = useAuth();
	const { data, isLoading, isError } = useTopAnomalies(runId, 100);
	const [showAll, setShowAll] = useState(false);
	const [pageSize, setPageSize] = useState(25);
	const [page, setPage] = useState(0);

	if (isLoading) return <LoadingText>Loading anomalies…</LoadingText>;
	if (isError)
		return <ErrorText>Failed to load anomalies for this run.</ErrorText>;

	const rows = data?.candidates ?? [];
	if (!rows.length)
		return <EmptyText>No flagged transactions in this run.</EmptyText>;

	const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
	const currentPage = Math.min(page, pageCount - 1);
	const pageRows = rows.slice(
		currentPage * pageSize,
		currentPage * pageSize + pageSize,
	);

	return (
		<div className="border-border overflow-hidden rounded-md border">
			<div className="border-border flex items-center justify-between gap-3 border-b px-4 py-2">
				<p className="text-muted-foreground text-xs">
					Rows flagged by ≥{STRONG_VOTE_THRESHOLD} detectors are highlighted;
					the rest are ranked by consensus.
				</p>
				<label className="text-muted-foreground flex items-center gap-1.5 text-xs select-none">
					<input
						type="checkbox"
						className="accent-primary h-3.5 w-3.5"
						checked={showAll}
						onChange={(e) => setShowAll(e.target.checked)}
					/>
					Show all features
				</label>
			</div>
			<Table>
				<TableHeader>
					<TableRow className="hover:bg-transparent">
						<TableHead className="text-right">#</TableHead>
						<TableHead>Transaction</TableHead>
						<TableHead>Consensus</TableHead>
						<TableHead className="text-right">Votes</TableHead>
						<TableHead className="text-right" title="Isolation Forest score">
							Iso
						</TableHead>
						<TableHead className="text-right" title="Local Outlier Factor">
							Lof
						</TableHead>
						<TableHead
							className="text-center"
							title="✓ when DBSCAN labelled this tx as noise"
						>
							Dbscan
						</TableHead>
						<TableHead>Verdict</TableHead>
						{/* Per-tx labelling is Admin-only; hide the column for a
						    read-only Reviewer rather than show an empty one. */}
						{isAdmin && <TableHead className="text-right">Label</TableHead>}
						<TableHead className="text-right">Fee (₳)</TableHead>
						{showAll && <TableHead className="text-right">Size</TableHead>}
						{showAll && <TableHead className="text-right">In (₳)</TableHead>}
						<TableHead className="text-right">Out (₳)</TableHead>
						{showAll && <TableHead className="text-right">Net (₳)</TableHead>}
						<TableHead className="text-right">In/Out</TableHead>
						<TableHead className="text-right">Assets</TableHead>
						{showAll && <TableHead className="text-right">Redeemers</TableHead>}
						{showAll && <TableHead className="text-right">Hour</TableHead>}
						{showAll && <TableHead>Day</TableHead>}
					</TableRow>
				</TableHeader>
				<TableBody>
					{pageRows.map((a: AnomalyCandidate) => (
						<TableRow
							key={a.tx_hash}
							className={cn(
								a.votes >= STRONG_VOTE_THRESHOLD &&
									"bg-severity-high/5 border-severity-high-foreground/30",
							)}
						>
							<TableCell className="text-muted-foreground text-right tabular-nums">
								{a.score_rank}
							</TableCell>
							<TableCell>
								<CopyHash hash={a.tx_hash} />
								<ReasonChips reasons={a.reasons} />
							</TableCell>
							<TableCell>
								<div className="flex items-center gap-2">
									<div className="bg-muted h-1.5 w-20 overflow-hidden rounded">
										<div
											className="bg-brand h-full"
											style={{ width: `${Math.round(a.consensus * 100)}%` }}
										/>
									</div>
									<span className="text-muted-foreground text-xs tabular-nums">
										{a.consensus.toFixed(2)}
									</span>
								</div>
							</TableCell>
							<TableCell
								className={cn(
									"text-right tabular-nums",
									a.votes >= STRONG_VOTE_THRESHOLD && "font-semibold",
								)}
							>
								{a.votes}
							</TableCell>
							<TableCell className="text-muted-foreground text-right tabular-nums">
								{a.iso_score === null ? "—" : a.iso_score.toFixed(2)}
							</TableCell>
							<TableCell className="text-muted-foreground text-right tabular-nums">
								{a.lof_score.toFixed(2)}
							</TableCell>
							<TableCell className="text-center">
								{a.dbscan_noise ? <span aria-label="DBSCAN noise">✓</span> : ""}
							</TableCell>
							<TableCell>
								<VerdictBadge verdict={a.verdict} />
							</TableCell>
							{isAdmin && (
								<TableCell className="text-right">
									<TxLabelControl
										target={target}
										txHash={a.tx_hash}
										ownLabel={a.label}
									/>
								</TableCell>
							)}
							<TableCell className="text-right tabular-nums">
								{formatAdaExact(a.fees, 0)}
							</TableCell>
							{showAll && (
								<TableCell className="text-right tabular-nums">
									{formatInt(a.size)}
								</TableCell>
							)}
							{showAll && (
								<TableCell className="text-right tabular-nums">
									{formatAdaExact(a.total_input_lovelace, 0)}
								</TableCell>
							)}
							<TableCell className="text-right tabular-nums">
								{formatAdaExact(a.total_output_lovelace, 0)}
							</TableCell>
							{showAll && (
								<TableCell className="text-right tabular-nums">
									{formatAdaExact(a.net_lovelace, 0)}
								</TableCell>
							)}
							<TableCell className="text-right tabular-nums">
								{a.input_count}/{a.output_count}
							</TableCell>
							<TableCell className="text-right tabular-nums">
								{a.distinct_assets}
							</TableCell>
							{showAll && (
								<TableCell className="text-right tabular-nums">
									{a.redeemer_count}
								</TableCell>
							)}
							{showAll && (
								<TableCell className="text-right tabular-nums">
									{a.hour_of_day}
								</TableCell>
							)}
							{showAll && (
								<TableCell>
									{WEEKDAYS[a.day_of_week] ?? a.day_of_week}
								</TableCell>
							)}
						</TableRow>
					))}
				</TableBody>
			</Table>
			<TableFooter
				pageSize={pageSize}
				onPageSizeChange={(n) => {
					setPageSize(n);
					setPage(0);
				}}
				pageSizeOptions={[10, 25, 50]}
				centerLabel={`${rows.length} flagged / ranked transactions`}
				page={currentPage}
				pageCount={pageCount}
				onPageChange={setPage}
			/>
		</div>
	);
}
