/** Per-cluster summary for a run, with verdict labelling (reskinned to TMS). */
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import {
	type ClusterSummary,
	useClearClusterLabel,
	useClusterSummary,
	useLabelCluster,
} from "@/lib/api/clustering";

const ADA = 1_000_000;

function clusterName(id: number): string {
	return id < 0 ? "Noise" : `Cluster ${id}`;
}

export function ClusterSummaryTable({ runId }: { runId: string }) {
	const { data: clusters, isLoading } = useClusterSummary(runId);
	const label = useLabelCluster();
	const clear = useClearClusterLabel();

	if (isLoading) return <p className="text-sm text-muted-foreground">Loading clusters…</p>;
	if (!clusters?.length)
		return <p className="text-sm text-muted-foreground">No clusters in this run.</p>;

	return (
		<Table>
			<TableHeader>
				<TableRow>
					<TableHead>Cluster</TableHead>
					<TableHead className="text-right">Size</TableHead>
					<TableHead className="text-right">Anomalies</TableHead>
					<TableHead className="text-right">Avg fee</TableHead>
					<TableHead className="text-right">Avg out (₳)</TableHead>
					<TableHead className="text-right">Avg in/out</TableHead>
					<TableHead>Verdict</TableHead>
					<TableHead className="text-right">Label</TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				{clusters.map((c: ClusterSummary) => (
					<TableRow key={c.cluster_id}>
						<TableCell className="font-medium">{clusterName(c.cluster_id)}</TableCell>
						<TableCell className="text-right tabular-nums">{c.size}</TableCell>
						<TableCell className="text-right tabular-nums">
							{c.anomaly_count > 0 ? (
								<Badge variant="high">{c.anomaly_count}</Badge>
							) : (
								<span className="text-muted-foreground">0</span>
							)}
						</TableCell>
						<TableCell className="text-right tabular-nums">
							{Math.round(c.avg_fees).toLocaleString()}
						</TableCell>
						<TableCell className="text-right tabular-nums">
							{(c.avg_output_lovelace / ADA).toLocaleString(undefined, {
								maximumFractionDigits: 1,
							})}
						</TableCell>
						<TableCell className="text-right tabular-nums">
							{c.avg_inputs.toFixed(1)} / {c.avg_outputs.toFixed(1)}
						</TableCell>
						<TableCell>
							{c.verdict ? (
								<Badge variant={c.verdict === "malicious" ? "critical" : "low"}>
									{c.verdict}
								</Badge>
							) : (
								<span className="text-muted-foreground">—</span>
							)}
						</TableCell>
						<TableCell className="text-right">
							{c.cluster_id < 0 ? (
								<span className="text-muted-foreground">—</span>
							) : c.verdict ? (
								<Button
									variant="ghost"
									size="sm"
									onClick={() => clear.mutate({ runId, clusterId: c.cluster_id })}
								>
									Clear
								</Button>
							) : (
								<div className="flex justify-end gap-1">
									<Button
										variant="ghost"
										size="sm"
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
					</TableRow>
				))}
			</TableBody>
		</Table>
	);
}
