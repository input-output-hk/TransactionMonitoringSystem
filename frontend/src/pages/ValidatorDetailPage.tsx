/**
 * Watched-contract detail: the clustering drill-down for one validator. The
 * surfaces (graph, clusters, anomalies, latest interactions) are split across
 * tabs; each tab's data fetch is gated by mount, since Radix unmounts the
 * inactive panels, so heavy work (Cytoscape, large fetches) runs only when its
 * tab is open. Contracts are fitted/classified automatically by the sidecar.
 */
import { Suspense, lazy, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { AnomalyHelp } from "@/components/clustering/AnomalyHelp";
import { AnomalyRunControls } from "@/components/clustering/AnomalyRunControls";
import { AnomalyTable } from "@/components/clustering/AnomalyTable";
import { ClusterSummaryTable } from "@/components/clustering/ClusterSummaryTable";
import { LatestTable } from "@/components/clustering/LatestTable";
import { TuningPanel } from "@/components/clustering/TuningPanel";
// Cytoscape is heavy (~500 kB); code-split it so it loads only when the Graph
// tab is opened, keeping it out of the main bundle.
const ClusterGraph = lazy(() =>
	import("@/components/clustering/ClusterGraph").then((m) => ({
		default: m.ClusterGraph,
	})),
);
// Plotly is ~1 MB; code-split it so it loads only when the Projection tab opens.
const ProjectionScatter = lazy(
	() => import("@/components/clustering/ProjectionScatter"),
);

// Tab ids, also the accepted `?tab=` values; an unknown value falls back to graph.
const TAB_VALUES = [
	"graph",
	"projection",
	"clusters",
	"anomalies",
	"latest",
	"tuning",
] as const;
import { VerdictLegend } from "@/components/clustering/VerdictLegend";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
	type Run,
	useAnomalyRuns,
	useClusterGraph,
	useRuns,
} from "@/lib/api/clustering";

/** Graph surface for a run. Self-contained so its fetch + Cytoscape mount only
 *  happen while the Graph tab is selected. A node tap focuses the cluster in the
 *  Clusters tab. */
function GraphTab({
	runId,
	onFocusCluster,
}: {
	runId: string;
	onFocusCluster: (clusterId: number) => void;
}) {
	const { data: graph, isLoading, isError } = useClusterGraph(runId);

	if (isError)
		return (
			<p className="text-destructive text-sm">
				Failed to load the cluster graph. The clustering service may be
				unavailable.
			</p>
		);
	if (isLoading)
		return <p className="text-muted-foreground text-sm">Building graph…</p>;
	if (!graph || !graph.nodes.length)
		return (
			<p className="text-muted-foreground text-sm">
				No graph available for this run.
			</p>
		);

	return (
		<>
			<div className="border-border h-[460px] overflow-hidden rounded-md border">
				<Suspense
					fallback={
						<p className="text-muted-foreground p-4 text-sm">
							Loading graph view…
						</p>
					}
				>
					<ClusterGraph
						data={graph}
						onNodeTap={(_tx, cluster) => onFocusCluster(cluster)}
					/>
				</Suspense>
			</div>
			<div className="mt-2 flex flex-wrap items-center justify-between gap-2">
				<VerdictLegend />
				{graph.truncated && (
					<p className="text-muted-foreground text-xs">
						Showing {graph.shown.toLocaleString()} of{" "}
						{graph.total.toLocaleString()} transactions.
					</p>
				)}
			</div>
		</>
	);
}

/** Anomalies surface: pick which run to view (system run by default), manage
 *  custom runs, and show the selected run's ranked candidates. */
function AnomaliesTab({ target }: { target: string }) {
	const { data: runs, isLoading, isError } = useAnomalyRuns(target);
	const [selectedRunId, setSelectedRunId] = useState("");

	// Default to the canonical system run (prefer the shape feature set), so the
	// view always opens on the run that drives scoring. `selectedRunId || default`
	// avoids an effect to seed the selection.
	const defaultRun = useMemo(
		() =>
			runs?.find((r) => r.origin === "system" && r.feature_set === "shape") ??
			runs?.find((r) => r.origin === "system") ??
			runs?.[0],
		[runs],
	);
	const effectiveRunId = selectedRunId || defaultRun?.run_id || "";
	const selectedRun = runs?.find((r) => r.run_id === effectiveRunId);

	if (isLoading)
		return (
			<p className="text-muted-foreground text-sm">Loading anomaly runs…</p>
		);
	if (isError)
		return (
			<p className="text-destructive text-sm">Failed to load anomaly runs.</p>
		);

	return (
		<div className="space-y-4">
			<AnomalyRunControls
				target={target}
				runs={runs ?? []}
				selectedRunId={effectiveRunId}
				onSelectRun={setSelectedRunId}
			/>
			{selectedRun && (
				<p className="text-muted-foreground text-sm">
					{selectedRun.n_flagged} flagged of{" "}
					{selectedRun.n_points.toLocaleString()} · detectors:{" "}
					{selectedRun.methods || "—"}
				</p>
			)}

			<AnomalyHelp showColumnKey={!!effectiveRunId} />
			{effectiveRunId ? (
				<AnomalyTable runId={effectiveRunId} target={target} />
			) : (
				<p className="text-muted-foreground text-sm">
					No anomaly run yet. Use “Detect anomalies” to score every transaction.
				</p>
			)}
		</div>
	);
}

export function ValidatorDetailPage() {
	const { target = "" } = useParams();
	const decoded = decodeURIComponent(target);

	const { data: runs, isError: runsError } = useRuns(decoded);

	// Tab lives in the URL (?tab=) so card deep-links ("Explore", "Outliers") and
	// browser navigation land on the right surface.
	const [searchParams, setSearchParams] = useSearchParams();
	const tabParam = searchParams.get("tab") ?? "";
	const tab = (TAB_VALUES as readonly string[]).includes(tabParam)
		? tabParam
		: "graph";
	const setTab = (value: string) =>
		setSearchParams(
			(prev) => {
				const next = new URLSearchParams(prev);
				next.set("tab", value);
				return next;
			},
			{ replace: true },
		);
	// Lifted so a graph click can focus a cluster in the Clusters tab.
	const [selectedCluster, setSelectedCluster] = useState<number | null>(null);

	// Runs come newest-first; the latest shape run backs the clusters + graph.
	const latestRun = useMemo<Run | undefined>(
		() => runs?.find((r) => r.feature_set === "shape") ?? runs?.[0],
		[runs],
	);

	const focusCluster = (clusterId: number) => {
		setSelectedCluster(clusterId);
		setTab("clusters");
	};

	return (
		<div className="space-y-6">
			<div>
				<Link to="/validators" className="text-brand text-sm hover:underline">
					← Watched Validators
				</Link>
				<div className="mt-1 flex flex-wrap items-center gap-2">
					<h1 className="font-mono text-lg font-semibold break-all">
						{decoded}
					</h1>
					{latestRun && (
						<Badge
							variant={latestRun.origin === "system" ? "outline" : "medium"}
						>
							{latestRun.origin === "system" ? "System" : "Custom"}
						</Badge>
					)}
				</div>
				{latestRun && (
					<p className="text-muted-foreground text-sm">
						{latestRun.n_points.toLocaleString()} transactions ·{" "}
						{latestRun.n_clusters} clusters · {latestRun.n_noise} noise
						{latestRun.silhouette != null &&
							` · silhouette ${latestRun.silhouette.toFixed(3)}`}
					</p>
				)}
			</div>

			{runsError ? (
				<Card>
					<CardContent className="text-destructive py-8 text-center text-sm">
						Could not load clustering runs for this contract. The clustering
						service may be unavailable; retry shortly.
					</CardContent>
				</Card>
			) : !latestRun ? (
				<Card>
					<CardContent className="text-muted-foreground py-8 text-center text-sm">
						No clustering run yet for this contract. The sidecar fits it
						automatically once enough transactions have been ingested.
					</CardContent>
				</Card>
			) : (
				<Tabs value={tab} onValueChange={setTab}>
					<TabsList>
						<TabsTrigger value="graph">Graph</TabsTrigger>
						<TabsTrigger value="projection">Projection</TabsTrigger>
						<TabsTrigger value="clusters">Clusters</TabsTrigger>
						<TabsTrigger value="anomalies">Anomalies</TabsTrigger>
						<TabsTrigger value="latest">Latest</TabsTrigger>
						<TabsTrigger value="tuning">Tuning</TabsTrigger>
					</TabsList>

					<TabsContent value="graph">
						<GraphTab runId={latestRun.run_id} onFocusCluster={focusCluster} />
					</TabsContent>

					<TabsContent value="projection">
						<Suspense
							fallback={
								<p className="text-muted-foreground text-sm">
									Loading projection view…
								</p>
							}
						>
							<ProjectionScatter runId={latestRun.run_id} />
						</Suspense>
					</TabsContent>

					<TabsContent value="clusters">
						<ClusterSummaryTable
							runId={latestRun.run_id}
							target={decoded}
							selectedCluster={selectedCluster}
							onSelectCluster={setSelectedCluster}
						/>
					</TabsContent>

					<TabsContent value="anomalies">
						<AnomaliesTab target={decoded} />
					</TabsContent>

					<TabsContent value="latest">
						<LatestTable target={decoded} />
					</TabsContent>

					<TabsContent value="tuning">
						<p className="text-muted-foreground mb-4 text-sm">
							Advanced, experimental controls. The system-tuned run remains
							canonical for scoring; runs you create here are kept separate and
							badged <span className="font-medium">Custom</span>.
						</p>
						<TuningPanel target={decoded} activeRun={latestRun ?? null} />
					</TabsContent>
				</Tabs>
			)}
		</div>
	);
}
