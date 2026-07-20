/** Per-cluster summary for a run, with verdict labelling and a row-expand
 *  drill-down into each cluster's transactions (reskinned to the TMS theme). */
import { Fragment, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyText, LoadingText } from "@/components/ui/status-text";
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import {
	Tooltip,
	TooltipContent,
	TooltipTrigger,
} from "@/components/ui/tooltip";
import {
	type ClusterSummary,
	useClearClusterLabel,
	useClusterSummary,
	useLabelCluster,
} from "@/lib/api/clustering";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ClusterTag } from "./cells";
import { ClusterTransactions } from "./ClusterTransactions";
import { formatInt } from "./format";
import { formatAdaExact } from "@/lib/utils/numbers";
import { VERDICT_BADGE, VERDICT_LABEL } from "./verdict";

// The grid has 9 columns; the expanded drill-down row spans all of them. The
// last one (Label) is Admin-only, so a Reviewer's grid is one column narrower.
const COLUMN_COUNT = 9;
const COLUMN_COUNT_READONLY = COLUMN_COUNT - 1; // Label column hidden

type Props = {
	runId: string;
	target: string;
	// Lifted selection so a graph/scatter click can focus a cluster here.
	selectedCluster?: number | null;
	onSelectCluster?: (clusterId: number | null) => void;
};

export function ClusterSummaryTable({
	runId,
	target,
	selectedCluster,
	onSelectCluster,
}: Props) {
	const { isAdmin } = useAuth();
	const { data: clusters, isLoading } = useClusterSummary(runId);
	const label = useLabelCluster();
	const clear = useClearClusterLabel();
	const columnCount = isAdmin ? COLUMN_COUNT : COLUMN_COUNT_READONLY;
	// Uncontrolled fallback when the parent doesn't lift selection.
	const [localExpanded, setLocalExpanded] = useState<number | null>(null);
	const expanded =
		selectedCluster !== undefined ? selectedCluster : localExpanded;
	const setExpanded = (id: number | null) =>
		onSelectCluster ? onSelectCluster(id) : setLocalExpanded(id);

	if (isLoading) return <LoadingText>Loading clusters…</LoadingText>;
	if (!clusters?.length) return <EmptyText>No clusters in this run.</EmptyText>;

	const toggle = (id: number) => setExpanded(expanded === id ? null : id);

	return (
		<Table>
			<TableHeader>
				<TableRow className="hover:bg-transparent">
					<TableHead className="w-8" />
					<TableHead>Cluster</TableHead>
					<TableHead className="text-right">Size</TableHead>
					<TableHead className="text-right">Anomalies</TableHead>
					<TableHead className="text-right">Avg fee (₳)</TableHead>
					<TableHead className="text-right">Avg out (₳)</TableHead>
					<TableHead className="text-right">Avg in/out</TableHead>
					<TableHead>Verdict</TableHead>
					{/* Labelling is an Admin-only mutation; hide the column for a
					    read-only Reviewer rather than show an empty one. */}
					{isAdmin && <TableHead className="text-right">Label</TableHead>}
				</TableRow>
			</TableHeader>
			<TableBody>
				{clusters.map((c: ClusterSummary) => {
					const isOpen = expanded === c.cluster_id;
					return (
						<Fragment key={c.cluster_id}>
							<TableRow
								className="cursor-pointer"
								onClick={() => toggle(c.cluster_id)}
							>
								<TableCell className="text-muted-foreground">
									{isOpen ? (
										<ChevronDown className="h-4 w-4" />
									) : (
										<ChevronRight className="h-4 w-4" />
									)}
								</TableCell>
								<TableCell className="font-medium">
									<ClusterTag clusterId={c.cluster_id} />
								</TableCell>
								<TableCell className="text-right tabular-nums">
									{formatInt(c.size)}
								</TableCell>
								<TableCell className="text-right tabular-nums">
									{c.anomaly_count > 0 ? (
										<Badge variant="high">{c.anomaly_count}</Badge>
									) : (
										<span className="text-muted-foreground">0</span>
									)}
								</TableCell>
								<TableCell className="text-right tabular-nums">
									{formatAdaExact(c.avg_fees, 2)}
								</TableCell>
								<TableCell className="text-right tabular-nums">
									{formatAdaExact(c.avg_output_lovelace, 1)}
								</TableCell>
								<TableCell className="text-right tabular-nums">
									{c.avg_inputs.toFixed(1)} / {c.avg_outputs.toFixed(1)}
								</TableCell>
								<TableCell>
									<span className="flex items-center gap-1">
										{c.verdict ? (
											<Badge
												variant={c.verdict === "malicious" ? "critical" : "low"}
											>
												{VERDICT_LABEL[c.verdict]}
											</Badge>
										) : c.anomaly_count > 0 ? (
											<Badge variant={VERDICT_BADGE.anomaly}>
												{VERDICT_LABEL.anomaly}
											</Badge>
										) : (
											<span className="text-muted-foreground">—</span>
										)}
										{c.verdict_conflict && (
											<Tooltip>
												<TooltipTrigger asChild>
													<span className="text-severity-high-foreground cursor-help">
														⚠
													</span>
												</TooltipTrigger>
												<TooltipContent>
													This cluster has both malicious- and benign-labeled
													transactions; malicious wins.
												</TooltipContent>
											</Tooltip>
										)}
									</span>
								</TableCell>
								{isAdmin && (
									<TableCell
										className="text-right"
										onClick={(e) => e.stopPropagation()}
									>
										{c.cluster_id < 0 ? (
											// Noise bucket: nothing to label.
											<span className="text-muted-foreground">—</span>
										) : c.verdict ? (
											<Button
												variant="ghost"
												size="sm"
												disabled={clear.isPending}
												onClick={() =>
													clear.mutate({ runId, clusterId: c.cluster_id })
												}
											>
												Clear
											</Button>
										) : (
											<div className="flex justify-end gap-1">
												<Button
													variant="ghost"
													size="sm"
													disabled={label.isPending}
													onClick={() =>
														label.mutate({
															runId,
															clusterId: c.cluster_id,
															verdict: "malicious",
														})
													}
												>
													Malicious
												</Button>
												<Button
													variant="ghost"
													size="sm"
													disabled={label.isPending}
													onClick={() =>
														label.mutate({
															runId,
															clusterId: c.cluster_id,
															verdict: "benign",
														})
													}
												>
													Benign
												</Button>
											</div>
										)}
									</TableCell>
								)}
							</TableRow>
							{isOpen && (
								<TableRow className={cn("hover:bg-transparent")}>
									<TableCell colSpan={columnCount} className="p-2">
										<ClusterTransactions
											runId={runId}
											target={target}
											clusterId={c.cluster_id}
										/>
									</TableCell>
								</TableRow>
							)}
						</Fragment>
					);
				})}
			</TableBody>
		</Table>
	);
}
