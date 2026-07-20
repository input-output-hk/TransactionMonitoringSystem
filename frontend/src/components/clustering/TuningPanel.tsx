/**
 * Advanced, secondary clustering controls: evaluate DBSCAN parameters for a
 * target/feature-set (k-distance curve + grid), then run a CUSTOM clustering
 * with chosen parameters. The system-tuned run remains canonical for scoring;
 * a custom run is for experimentation only and is badged as such.
 */
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
	useEvaluation,
	useRunCluster,
} from "@/lib/api/clustering";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { AdminOnlyGate } from "./adminOnly";
import { FeatureSetSelect } from "./FeatureSetSelect";
import { KDistanceChart } from "./KDistanceChart";

function fmtScore(v: number | null): string {
	return v === null || Number.isNaN(v) ? "—" : v.toFixed(3);
}

export function TuningPanel({
	target,
	activeRun,
}: {
	target: string;
	activeRun: Run | null;
}) {
	const [featureSet, setFeatureSet] = useState<FeatureSet>(
		activeRun?.feature_set ?? "shape",
	);
	const [eps, setEps] = useState<number>(activeRun?.eps ?? 0.5);
	const [minSamples, setMinSamples] = useState<number>(
		activeRun?.min_samples ?? 5,
	);

	const { isAdmin } = useAuth();
	const evaluation = useEvaluation(target, featureSet);
	const runCluster = useRunCluster();
	const evalData = evaluation.data;
	const rec = evalData?.recommended;
	const isSystem = activeRun?.origin === "system";

	const applyParams = (e: number, m: number) => {
		setEps(e);
		setMinSamples(m);
	};

	return (
		<div className="space-y-6">
			{/* Current run summary */}
			<div className="border-border space-y-2 rounded-md border p-4">
				<div className="flex items-center gap-2">
					<h3 className="text-foreground text-sm font-semibold">
						Active clustering
					</h3>
					{activeRun && (
						<Badge variant={isSystem ? "outline" : "medium"}>
							{isSystem ? "System-tuned" : "Custom"}
						</Badge>
					)}
				</div>
				{activeRun ? (
					<>
						<div className="text-muted-foreground grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
							<span>
								Feature set:{" "}
								<span className="text-foreground">{activeRun.feature_set}</span>
							</span>
							<span>
								eps: <span className="text-foreground">{activeRun.eps}</span>
							</span>
							<span>
								min_samples:{" "}
								<span className="text-foreground">{activeRun.min_samples}</span>
							</span>
							<span>
								clusters:{" "}
								<span className="text-foreground">{activeRun.n_clusters}</span>
							</span>
						</div>
						<p className="text-muted-foreground text-xs">
							{isSystem
								? "Parameters were tuned automatically. Use the controls below to try alternatives; the system-tuned run remains canonical for scoring."
								: "A custom clustering. The system-tuned run remains canonical for scoring."}
						</p>
					</>
				) : (
					<p className="text-muted-foreground text-sm">
						No clustering run yet.
					</p>
				)}
			</div>

			{/* Advanced controls */}
			<div className="space-y-4">
				<div className="flex flex-wrap items-end gap-3">
					<div className="w-44">
						<Label
							htmlFor="tuning-feature-set"
							className="mb-1.5 block text-xs"
						>
							Feature set
						</Label>
						<FeatureSetSelect
							id="tuning-feature-set"
							value={featureSet}
							onChange={setFeatureSet}
						/>
					</div>
					<Button
						variant="outline"
						disabled={evaluation.isFetching}
						onClick={() => void evaluation.refetch()}
					>
						{evaluation.isFetching ? "Evaluating…" : "Evaluate parameters"}
					</Button>
				</div>

				<HelpDetails summary="Which feature set?">
					<p>This decides what is compared to group (or score) transactions:</p>
					<ul>
						<li>
							<strong>shape:</strong> each tx as 13 numbers (fees, size, in/out
							counts, ADA in/out and net, distinct assets, redeemers,
							time-of-day). Groups txs that look alike regardless of who's
							involved. Euclidean distance; fast, the default.
						</li>
						<li>
							<strong>graph:</strong> compares the set of addresses each tx
							touches (Jaccard distance). Finds txs sharing counterparties
							(entity / co-spend structure). O(n²), capped at 5000 txs;
							Isolation Forest can't run on it (anomaly votes max out at 2
							there).
						</li>
						<li>
							<strong>combined:</strong> shape features plus a compact (SVD)
							embedding of the address graph. Both signals at once, and scales
							better than graph for large targets.
						</li>
					</ul>
				</HelpDetails>

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
							its parameters below.
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

				<div className="flex flex-wrap items-end gap-3">
					<div className="w-32">
						<Label htmlFor="tuning-eps" className="mb-1.5 block text-xs">
							eps
						</Label>
						<Input
							id="tuning-eps"
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
					<div className="w-32">
						<Label
							htmlFor="tuning-min-samples"
							className="mb-1.5 block text-xs"
						>
							min_samples
						</Label>
						<Input
							id="tuning-min-samples"
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
							onClick={() =>
								runCluster.mutate({
									target,
									feature_set: featureSet,
									eps,
									min_samples: minSamples,
								})
							}
						>
							{runCluster.isPending
								? "Running DBSCAN…"
								: "Run custom clustering"}
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
				{runCluster.isSuccess && runCluster.data && (
					<p className="text-muted-foreground text-sm">
						Created a <Badge variant="medium">Custom</Badge> run:{" "}
						{runCluster.data.n_clusters} clusters, {runCluster.data.n_noise}{" "}
						noise over {runCluster.data.n_points.toLocaleString()} txs. The
						system-tuned run remains canonical for scoring.
					</p>
				)}
			</div>
		</div>
	);
}
