/** Top anomaly candidates for a run, ranked by ensemble consensus (TMS-reskinned). */
import { Badge } from "@/components/ui/badge";
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import { type AnomalyCandidate, useTopAnomalies } from "@/lib/api/clustering";
import { VERDICT_BADGE, VERDICT_LABEL } from "./verdict";

function shortHash(h: string): string {
	return `${h.slice(0, 8)}…${h.slice(-6)}`;
}

export function AnomalyTable({ runId }: { runId: string }) {
	const { data, isLoading } = useTopAnomalies(runId, 100);
	if (isLoading) return <p className="text-sm text-muted-foreground">Loading anomalies…</p>;
	const rows = data?.candidates ?? [];
	if (!rows.length)
		return (
			<p className="text-sm text-muted-foreground">
				No flagged transactions in this run.
			</p>
		);

	return (
		<Table>
			<TableHeader>
				<TableRow>
					<TableHead className="text-right">#</TableHead>
					<TableHead>Transaction</TableHead>
					<TableHead>Consensus</TableHead>
					<TableHead className="text-right">Votes</TableHead>
					<TableHead className="text-right">In/Out</TableHead>
					<TableHead className="text-right">Assets</TableHead>
					<TableHead>Verdict</TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				{rows.map((a: AnomalyCandidate) => (
					<TableRow key={a.tx_hash}>
						<TableCell className="text-right tabular-nums text-muted-foreground">
							{a.score_rank}
						</TableCell>
						<TableCell className="font-mono text-xs">{shortHash(a.tx_hash)}</TableCell>
						<TableCell>
							<div className="flex items-center gap-2">
								<div className="h-1.5 w-20 overflow-hidden rounded bg-muted">
									<div
										className="h-full bg-brand"
										style={{ width: `${Math.round(a.consensus * 100)}%` }}
									/>
								</div>
								<span className="tabular-nums text-xs text-muted-foreground">
									{a.consensus.toFixed(2)}
								</span>
							</div>
						</TableCell>
						<TableCell className="text-right tabular-nums">{a.votes}</TableCell>
						<TableCell className="text-right tabular-nums">
							{a.input_count}/{a.output_count}
						</TableCell>
						<TableCell className="text-right tabular-nums">{a.distinct_assets}</TableCell>
						<TableCell>
							<Badge variant={VERDICT_BADGE[a.verdict]}>
								{VERDICT_LABEL[a.verdict]}
							</Badge>
						</TableCell>
					</TableRow>
				))}
			</TableBody>
		</Table>
	);
}
