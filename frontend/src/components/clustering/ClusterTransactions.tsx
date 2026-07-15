/**
 * Per-cluster transaction drill-down: the transactions inside one cluster of a
 * run, with their effective verdict and a per-tx label control. Rendered inside
 * an expanded row of the cluster summary table.
 */
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import { type TxRow, useClusterTransactions } from "@/lib/api/clustering";
import { CopyHash, VerdictBadge } from "./cells";
import { formatAdaExact } from "@/lib/utils/numbers";
import { formatTimeAgo } from "@/lib/utils/dates";
import { TxLabelControl } from "./TxLabelControl";
import { EmptyText, ErrorText, LoadingText } from "@/components/ui/status-text";

type Props = { runId: string; target: string; clusterId: number };

export function ClusterTransactions({ runId, target, clusterId }: Props) {
	const { data, isLoading, isError } = useClusterTransactions(runId, clusterId);

	if (isLoading)
		return (
			<LoadingText className="px-4 py-3">Loading transactions…</LoadingText>
		);
	if (isError)
		return (
			<ErrorText className="px-4 py-3">Failed to load this cluster's transactions.</ErrorText>
		);

	const rows = data?.transactions ?? [];
	if (!rows.length)
		return (
			<EmptyText className="px-4 py-3">No transactions in this cluster.</EmptyText>
		);

	return (
		<div className="bg-muted/20 border-border/60 rounded-md border">
			<Table>
				<TableHeader>
					<TableRow className="hover:bg-transparent">
						<TableHead>Transaction</TableHead>
						<TableHead>Verdict</TableHead>
						<TableHead>Age</TableHead>
						<TableHead className="text-right">Fee (₳)</TableHead>
						<TableHead className="text-right">Out (₳)</TableHead>
						<TableHead className="text-right">In/Out</TableHead>
						<TableHead className="text-right">Assets</TableHead>
						<TableHead className="text-right">Label</TableHead>
					</TableRow>
				</TableHeader>
				<TableBody>
					{rows.map((t: TxRow) => (
						<TableRow key={t.tx_hash}>
							<TableCell>
								<CopyHash hash={t.tx_hash} />
							</TableCell>
							<TableCell>
								<VerdictBadge verdict={t.verdict} />
							</TableCell>
							<TableCell className="text-muted-foreground" title={t.block_time}>
								{formatTimeAgo(t.block_time, { compact: true })}
							</TableCell>
							<TableCell className="text-right tabular-nums">
								{formatAdaExact(t.fees, 0)}
							</TableCell>
							<TableCell className="text-right tabular-nums">
								{formatAdaExact(t.total_output_lovelace, 0)}
							</TableCell>
							<TableCell className="text-right tabular-nums">
								{t.input_count}/{t.output_count}
							</TableCell>
							<TableCell className="text-right tabular-nums">
								{t.distinct_assets}
							</TableCell>
							<TableCell className="text-right">
								<TxLabelControl
									target={target}
									txHash={t.tx_hash}
									ownLabel={t.label}
								/>
							</TableCell>
						</TableRow>
					))}
				</TableBody>
			</Table>
		</div>
	);
}
