/**
 * Watched-contract detail: the clustering drill-down for one validator, ported
 * from the engine's Explore view (cluster graph + cluster summary + anomaly
 * table) and reskinned to the TMS design system. Read-only: the contract is
 * fitted/classified automatically by the sidecar feed.
 */
import { Suspense, lazy, useMemo } from "react";
import { Link, useParams } from "react-router-dom";

import { AnomalyTable } from "@/components/clustering/AnomalyTable";
import { ClusterSummaryTable } from "@/components/clustering/ClusterSummaryTable";
// Cytoscape is heavy (~500 kB); code-split it so it loads only when an analyst
// opens a validator's detail view, keeping it out of the main bundle.
const ClusterGraph = lazy(() =>
	import("@/components/clustering/ClusterGraph").then((m) => ({
		default: m.ClusterGraph,
	})),
);
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
	useAnomalyRuns,
	useClusterGraph,
	useRuns,
} from "@/lib/api/clustering";

export function ValidatorDetailPage() {
	const { target = "" } = useParams();
	const decoded = decodeURIComponent(target);

	const { data: runs, isError: runsError } = useRuns(decoded);
	const { data: anomalyRuns } = useAnomalyRuns(decoded);

	// Runs come newest-first; the latest shape run backs the clusters + graph.
	const latestRun = useMemo(
		() => runs?.find((r) => r.feature_set === "shape") ?? runs?.[0],
		[runs],
	);
	const latestAnomalyRun = useMemo(
		() => anomalyRuns?.find((r) => r.feature_set === "shape") ?? anomalyRuns?.[0],
		[anomalyRuns],
	);
	const {
		data: graph,
		isLoading: graphLoading,
		isError: graphError,
	} = useClusterGraph(latestRun?.run_id);

	return (
		<div className="space-y-6">
			<div>
				<Link to="/validators" className="text-sm text-brand hover:underline">
					← Watched Validators
				</Link>
				<h1 className="mt-1 break-all font-mono text-lg font-semibold">{decoded}</h1>
				{latestRun && (
					<p className="text-sm text-muted-foreground">
						{latestRun.n_points.toLocaleString()} transactions ·{" "}
						{latestRun.n_clusters} clusters · {latestRun.n_noise} noise
						{latestRun.silhouette != null &&
							` · silhouette ${latestRun.silhouette.toFixed(3)}`}
					</p>
				)}
			</div>

			{runsError ? (
				<Card>
					<CardContent className="py-8 text-center text-sm text-destructive">
						Could not load clustering runs for this contract. The clustering
						service may be unavailable; retry shortly.
					</CardContent>
				</Card>
			) : !latestRun ? (
				<Card>
					<CardContent className="py-8 text-center text-sm text-muted-foreground">
						No clustering run yet for this contract. The sidecar fits it
						automatically once enough transactions have been ingested.
					</CardContent>
				</Card>
			) : (
				<>
					<Card>
						<CardHeader>
							<CardTitle>Cluster graph</CardTitle>
						</CardHeader>
						<CardContent>
							{graphError ? (
								<p className="text-sm text-destructive">
									Failed to load the cluster graph. The clustering service may
									be unavailable.
								</p>
							) : graphLoading ? (
								<p className="text-sm text-muted-foreground">Building graph…</p>
							) : graph && graph.nodes.length ? (
								<>
									<div className="h-[460px] overflow-hidden rounded-md border border-border">
										<Suspense
											fallback={
												<p className="p-4 text-sm text-muted-foreground">
													Loading graph view…
												</p>
											}
										>
											<ClusterGraph data={graph} />
										</Suspense>
									</div>
									{graph.truncated && (
										<p className="mt-2 text-xs text-muted-foreground">
											Showing {graph.shown.toLocaleString()} of{" "}
											{graph.total.toLocaleString()} transactions.
										</p>
									)}
								</>
							) : (
								<p className="text-sm text-muted-foreground">
									No graph available for this run.
								</p>
							)}
						</CardContent>
					</Card>

					<Card>
						<CardHeader>
							<CardTitle>Clusters</CardTitle>
						</CardHeader>
						<CardContent>
							<ClusterSummaryTable runId={latestRun.run_id} />
						</CardContent>
					</Card>

					{latestAnomalyRun && (
						<Card>
							<CardHeader>
								<CardTitle>
									Anomalies
									<span className="ml-2 text-sm font-normal text-muted-foreground">
										{latestAnomalyRun.n_flagged} flagged of{" "}
										{latestAnomalyRun.n_points.toLocaleString()}
									</span>
								</CardTitle>
							</CardHeader>
							<CardContent>
								<AnomalyTable runId={latestAnomalyRun.run_id} />
							</CardContent>
						</Card>
					)}
				</>
			)}
		</div>
	);
}
