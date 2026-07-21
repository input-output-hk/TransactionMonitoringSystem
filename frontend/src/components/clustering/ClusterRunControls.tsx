/**
 * Clustering run control bar: pick which cluster run the Graph / Projection /
 * Clusters views show, run a manual (custom) clustering pass, and delete a custom
 * run. The System run is canonical for scoring and is the default selection;
 * Delete is offered only for custom runs (and guarded server-side). Parameter
 * evaluation (k-distance curve + DBSCAN grid) lives in an "Advanced" disclosure so
 * it stays available without dominating. Sibling of `AnomalyRunControls`.
 */
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { HelpDetails } from "@/components/ui/help-details";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from "@/components/ui/table";
import {
	type FeatureSet,
	type Run,
	isPermissionDenied,
	useDeleteClusterRun,
	useEvaluation,
	useRunCluster,
} from "@/lib/api/clustering";
import { useAuth } from "@/lib/auth";
import { DOCS_CLUSTERING } from "@/lib/docs";
import { cn } from "@/lib/utils";
import { AdminOnlyGate } from "./adminOnly";
import { DocsCallout } from "./DocsCallout";
import { FeatureSetSelect } from "./FeatureSetSelect";
import { KDistanceChart } from "./KDistanceChart";
import { RunSelect } from "./RunSelect";

function fmtScore(v: number | null): string {
	return v === null || Number.isNaN(v) ? "—" : v.toFixed(3);
}

function runLabel(r: Run): string {
	return `${r.feature_set} · ${r.n_clusters} clusters`;
}

export function ClusterRunControls({
	target,
	runs,
	selectedRunId,
	onSelectRun,
}: {
	target: string;
	runs: Run[];
	selectedRunId: string;
	onSelectRun: (runId: string) => void;
}) {
	const activeRun = runs.find((r) => r.run_id === selectedRunId) ?? null;

	const [featureSet, setFeatureSet] = useState<FeatureSet>(
		activeRun?.feature_set ?? "shape",
	);
	const [eps, setEps] = useState<number>(activeRun?.eps ?? 0.5);
	const [minSamples, setMinSamples] = useState<number>(
		activeRun?.min_samples ?? 5,
	);
	const [confirmDelete, setConfirmDelete] = useState(false);

	const { isAdmin } = useAuth();
	const evaluation = useEvaluation(target, featureSet);
	const runCluster = useRunCluster();
	const remove = useDeleteClusterRun();
	const evalData = evaluation.data;
	const rec = evalData?.recommended;
	const isSystem = activeRun?.origin === "system";
	// Deleting a run is Admin-only at the proxy, and only custom runs may be
	// deleted (the system run is canonical for scoring).
	const canDelete = isAdmin && activeRun?.origin === "custom";

	const applyParams = (e: number, m: number) => {
		setEps(e);
		setMinSamples(m);
	};

	const onRecluster = () =>
		runCluster.mutate(
			{ target, feature_set: featureSet, eps, min_samples: minSamples },
			{ onSuccess: (run) => run?.run_id && onSelectRun(run.run_id) },
		);

	const onDelete = () => {
		if (!activeRun) return;
		remove.mutate(activeRun.run_id, {
			onSuccess: () => {
				setConfirmDelete(false);
				// Drop back to the default focus; the ["clustering"] invalidation
				// refetches the run list without the deleted run, so a stale
				// selection would otherwise point at a run that no longer exists.
				onSelectRun("");
			},
		});
	};

	return (
		<div className="border-border space-y-3 rounded-md border p-4">
			<DocsCallout href={DOCS_CLUSTERING}>
				Advanced control. A custom pass creates a separate run and never changes
				the canonical scoring, but its results need interpretation.
			</DocsCallout>

			{/* Run selection */}
			<div className="flex flex-wrap items-center gap-2">
				<span className="text-muted-foreground text-sm">Run</span>
				<RunSelect
					runs={runs}
					value={selectedRunId}
					onChange={onSelectRun}
					getLabel={runLabel}
				/>
				{activeRun && (
					<Badge variant={isSystem ? "outline" : "medium"}>
						{isSystem ? "System" : "Custom"}
					</Badge>
				)}
				{canDelete && (
					<Button
						variant="outline"
						size="sm"
						className="ml-auto"
						disabled={remove.isPending}
						onClick={() => setConfirmDelete(true)}
					>
						Delete run
					</Button>
				)}
			</div>

			{/* Create a custom run */}
			<div className="flex flex-wrap items-end gap-3">
				<div className="w-40">
					<Label htmlFor="cluster-feature-set" className="mb-1.5 block text-xs">
						Feature set
					</Label>
					<FeatureSetSelect
						id="cluster-feature-set"
						value={featureSet}
						onChange={setFeatureSet}
					/>
				</div>
				<div className="w-24">
					<Label htmlFor="cluster-eps" className="mb-1.5 block text-xs">
						eps
					</Label>
					<Input
						id="cluster-eps"
						type="number"
						step="0.01"
						min="0.0001"
						value={eps}
						onChange={(e) => {
							const v = Number.parseFloat(e.target.value);
							if (Number.isFinite(v)) setEps(v);
						}}
					/>
				</div>
				<div className="w-28">
					<Label htmlFor="cluster-min-samples" className="mb-1.5 block text-xs">
						min_samples
					</Label>
					<Input
						id="cluster-min-samples"
						type="number"
						step="1"
						min="2"
						value={minSamples}
						onChange={(e) => {
							const v = Number.parseInt(e.target.value, 10);
							if (Number.isFinite(v)) setMinSamples(v);
						}}
					/>
				</div>
				<AdminOnlyGate gated={!isAdmin}>
					<Button
						disabled={!isAdmin || runCluster.isPending || !eps || !minSamples}
						onClick={onRecluster}
					>
						{runCluster.isPending
							? "Clustering…"
							: "Re-cluster with custom parameters"}
					</Button>
				</AdminOnlyGate>
			</div>

			{runCluster.isError && (
				<p className="text-destructive text-sm">
					{isPermissionDenied(runCluster.error)
						? runCluster.error.message
						: "Clustering run failed. Check the parameters and retry."}
				</p>
			)}
			{remove.isError && (
				<p className="text-destructive text-sm">
					{isPermissionDenied(remove.error)
						? remove.error.message
						: "Could not delete the run. Retry shortly."}
				</p>
			)}

			<HelpDetails summary="What am I selecting?">
				<p>
					A <strong>run</strong> is one saved clustering pass over this
					validator's transactions. Each option reads{" "}
					<em>feature set · clusters · time</em>:
				</p>
				<ul>
					<li>
						<strong>origin:</strong> the <em>Canonical</em> run is the
						auto-tuned System run that drives scoring; a <em>Custom</em> run is
						an experiment you ran, kept separate and safe to delete.
					</li>
					<li>
						<strong>feature set:</strong> which signals are compared:{" "}
						<em>shape</em> (per-tx value, size, in/out counts, ADA moved, assets,
						time), <em>graph</em> (shared addresses), or <em>combined</em>.
					</li>
				</ul>
			</HelpDetails>

			<HelpDetails summary="Advanced: tune parameters">
				<div className="space-y-4 px-3 pb-3">
					<p className="text-muted-foreground text-xs">
						Evaluate DBSCAN parameters for the selected feature set, then click a
						row to load its <code>eps</code> / <code>min_samples</code> into the
						controls above.
					</p>
					<Button
						variant="outline"
						disabled={evaluation.isFetching}
						onClick={() => void evaluation.refetch()}
					>
						{evaluation.isFetching ? "Evaluating…" : "Evaluate parameters"}
					</Button>

					{evaluation.isError && (
						<p className="text-destructive text-sm">
							Evaluation failed. The clustering service may be slow or
							unavailable; retry shortly.
						</p>
					)}

					{evalData?.message && (
						<p className="text-muted-foreground text-sm">{evalData.message}</p>
					)}

					{evalData && !evalData.message && (
						<>
							<p className="text-muted-foreground text-xs">
								{evalData.n_points.toLocaleString()} txs
								{evalData.n_features !== null
									? ` · ${evalData.n_features} features`
									: ""}{" "}
								· {evalData.metric}
							</p>
							<KDistanceChart evaluation={evalData} />
							<HelpDetails summary="How to read the k-distance chart">
								<p>
									The line is the <strong>k-distance curve</strong>: for every tx
									we measure the distance to its <em>k</em>-th nearest neighbour
									(k = min_samples), then sort those distances low to high. Flat
									on the left = points sitting in dense neighbourhoods (cluster
									cores); the sharp rise on the right = increasingly isolated
									points (likely noise). The <strong>knee</strong> (dashed line)
									is where it turns up, a good starting <code>eps</code>, since it
									roughly separates "dense enough to cluster" from "too far
									apart".
								</p>
								<p>
									The table tries a few (eps, min_samples) pairs around that knee:
								</p>
								<ul>
									<li>
										<strong>eps:</strong> neighbourhood radius. Larger means
										fewer, bigger clusters and less noise.
									</li>
									<li>
										<strong>min_s</strong> (min_samples): points needed within{" "}
										<code>eps</code> to form a core. Larger is stricter, so more
										points fall out as noise.
									</li>
									<li>
										<strong>clusters:</strong> groups found (noise excluded).
									</li>
									<li>
										<strong>noise:</strong> share of txs left unassigned (DBSCAN's
										"−1" / outliers).
									</li>
									<li>
										<strong>silhouette:</strong> how clean the separation is, −1
										to 1 (higher is better); "—" when it can't be computed (e.g.
										fewer than 2 clusters).
									</li>
								</ul>
							</HelpDetails>
							<p className="text-muted-foreground text-xs">
								The highlighted row is the recommendation; click any row to load
								its parameters above.
							</p>
							<Table>
								<TableHeader>
									<TableRow className="hover:bg-transparent">
										<TableHead className="text-right">eps</TableHead>
										<TableHead className="text-right">min_s</TableHead>
										<TableHead className="text-right">clusters</TableHead>
										<TableHead className="text-right">noise</TableHead>
										<TableHead className="text-right">silhouette</TableHead>
									</TableRow>
								</TableHeader>
								<TableBody>
									{evalData.grid.map((g, i) => {
										const isRec =
											rec &&
											g.eps === rec.eps &&
											g.min_samples === rec.min_samples;
										return (
											<TableRow
												key={`${g.eps}-${g.min_samples}-${i}`}
												className={cn("cursor-pointer", isRec && "bg-brand/10")}
												onClick={() => applyParams(g.eps, g.min_samples)}
												title="Click to use these parameters"
											>
												<TableCell className="text-right tabular-nums">
													{g.eps}
												</TableCell>
												<TableCell className="text-right tabular-nums">
													{g.min_samples}
												</TableCell>
												<TableCell className="text-right tabular-nums">
													{g.n_clusters}
												</TableCell>
												<TableCell className="text-right tabular-nums">
													{(g.noise_ratio * 100).toFixed(1)}%
												</TableCell>
												<TableCell className="text-right tabular-nums">
													{fmtScore(g.silhouette)}
												</TableCell>
											</TableRow>
										);
									})}
								</TableBody>
							</Table>
							{rec && (
								<Button
									variant="link"
									className="px-0"
									onClick={() => applyParams(rec.eps, rec.min_samples)}
								>
									Use recommended (eps={rec.eps}, min_samples={rec.min_samples})
								</Button>
							)}
						</>
					)}
				</div>
			</HelpDetails>

			<Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
				<DialogContent>
					<DialogHeader>
						<DialogTitle>Delete this custom cluster run?</DialogTitle>
						<DialogDescription>
							This removes the run and its cluster labels. This cannot be undone.
							The system run is unaffected.
						</DialogDescription>
					</DialogHeader>
					<DialogFooter>
						<Button variant="outline" onClick={() => setConfirmDelete(false)}>
							Cancel
						</Button>
						<Button
							variant="destructive"
							disabled={remove.isPending}
							onClick={onDelete}
						>
							{remove.isPending ? "Deleting…" : "Delete"}
						</Button>
					</DialogFooter>
				</DialogContent>
			</Dialog>
		</div>
	);
}
