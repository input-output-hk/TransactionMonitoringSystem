/**
 * Watched-contract detail: the clustering drill-down for one validator. The
 * surfaces (graph, clusters, anomalies, latest interactions) are split across
 * tabs; each tab's data fetch is gated by mount, since Radix unmounts the
 * inactive panels, so heavy work (Cytoscape, large fetches) runs only when its
 * tab is open. Contracts are fitted/classified automatically by the sidecar.
 */
import { Suspense, lazy, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { qpEnum, useQueryParamState } from "@/lib/url-state";

import { AnomalyHelp } from "@/components/clustering/AnomalyHelp";
import { AnomalyRunControls } from "@/components/clustering/AnomalyRunControls";
import { AnomalyTable } from "@/components/clustering/AnomalyTable";
import { ClusterRunControls } from "@/components/clustering/ClusterRunControls";
import { ClusterSummaryTable } from "@/components/clustering/ClusterSummaryTable";
import { LatestTable } from "@/components/clustering/LatestTable";
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
] as const;

// Tabs that view a cluster run; the clustering control bar (run picker +
// re-cluster + delete) is shown above these and shares one run selection.
const CLUSTER_TABS: readonly string[] = ["graph", "projection", "clusters"];
import { VerdictLegend } from "@/components/clustering/VerdictLegend";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ErrorText, LoadingText } from "@/components/ui/status-text";
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
			<LoadingText>Loading anomaly runs…</LoadingText>
		);
	if (isError)
		return (
			<ErrorText>Failed to load anomaly runs.</ErrorText>
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
	// browser navigation land on the right surface. Read/write discipline is
	// shared with ReportsPage via lib/url-state.
	const { searchParams, setParam } = useQueryParamState();
	const tab = qpEnum(searchParams, "tab", TAB_VALUES, "graph");
	const setTab = (value: string) => setParam("tab", value);
	// Lifted so a graph click can focus a cluster in the Clusters tab.
	const [selectedCluster, setSelectedCluster] = useState<number | null>(null);
	// Which cluster run the Graph/Projection/Clusters views + control bar show.
	// Empty = follow the default (latest shape run); set by the run picker.
	const [selectedRunId, setSelectedRunId] = useState("");

	// Runs come newest-first; the latest shape run is the default view, backing
	// the clusters + graph until the analyst picks another via the control bar.
	const latestRun = useMemo<Run | undefined>(
		() => runs?.find((r) => r.feature_set === "shape") ?? runs?.[0],
		[runs],
	);
	// Validate the selection against the current list so a run that vanished
	// (deleted from another tab/session, or replaced by a re-onboard) can't leave
	// the views querying a dead id; fall back to the default run instead.
	const effectiveRunId = runs?.some((r) => r.run_id === selectedRunId)
		? selectedRunId
		: (latestRun?.run_id ?? "");
	const activeRun = runs?.find((r) => r.run_id === effectiveRunId) ?? latestRun;

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
					{activeRun && (
						<Badge
							variant={activeRun.origin === "system" ? "outline" : "medium"}
						>
							{activeRun.origin === "system" ? "System" : "Custom"}
						</Badge>
					)}
				</div>
				{activeRun && (
					<p className="text-muted-foreground text-sm">
						{activeRun.n_points.toLocaleString()} transactions ·{" "}
						{activeRun.n_clusters} clusters · {activeRun.n_noise} noise ·{" "}
						{activeRun.feature_set} · eps {activeRun.eps} · min_samples{" "}
						{activeRun.min_samples}
						{activeRun.silhouette != null &&
							` · silhouette ${activeRun.silhouette.toFixed(3)}`}
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
					</TabsList>

					{/* Shared clustering controls sit above the run's views (Graph /
					    Projection / Clusters), mirroring how Anomalies co-locates its
					    controls with its results. */}
					{CLUSTER_TABS.includes(tab) && (
						<div className="mt-4">
							{/* Key by the focused run so the create-run form (feature set /
							    eps / min_samples) re-seeds from whichever run is selected. */}
							<ClusterRunControls
								key={effectiveRunId}
								target={decoded}
								runs={runs ?? []}
								selectedRunId={effectiveRunId}
								onSelectRun={setSelectedRunId}
							/>
						</div>
					)}

					<TabsContent value="graph">
						<GraphTab runId={effectiveRunId} onFocusCluster={focusCluster} />
					</TabsContent>

					<TabsContent value="projection">
						<Suspense
							fallback={
								<p className="text-muted-foreground text-sm">
									Loading projection view…
								</p>
							}
						>
							<ProjectionScatter runId={effectiveRunId} />
						</Suspense>
					</TabsContent>

					<TabsContent value="clusters">
						<ClusterSummaryTable
							runId={effectiveRunId}
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
				</Tabs>
			)}
		</div>
	);
}
