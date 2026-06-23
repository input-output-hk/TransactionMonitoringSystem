/**
 * Feature-space projection of a run's transactions, rendered with Plotly. Each
 * point is a transaction placed by the same PCA/MDS projection DBSCAN clustered
 * on, so nearby points are genuinely similar. Coloured by cluster/verdict;
 * click a point to focus its cluster. 2-D uses SVG scatter (no WebGL); 3-D is
 * scatter3d. Default export so the host can code-split it behind the Projection
 * tab (Plotly is ~1 MB).
 */
import Plotly from "plotly.js-dist-min";
import { type ComponentType, useMemo, useState } from "react";
import type { PlotParams } from "react-plotly.js";
import factoryImport from "react-plotly.js/factory";

import { Button } from "@/components/ui/button";
import { useProjection } from "@/lib/api/clustering";
import { cn } from "@/lib/utils";
import { nodeColor } from "./verdict";

// Build the React wrapper from the prebuilt (MIT) plotly bundle via the factory,
// so we don't pull plotly.js's full source build into the chunk.
// `react-plotly.js/factory` is CJS: under some bundler interop the default
// import arrives wrapped as `{ default: fn }` rather than the fn itself. Unwrap
// defensively so both interop shapes work.
type PlotlyFactory = (plotly: object) => ComponentType<PlotParams>;
const createPlotlyComponent = ((
	factoryImport as unknown as { default?: PlotlyFactory }
).default ?? factoryImport) as PlotlyFactory;
const Plot = createPlotlyComponent(Plotly);

// Axis styling shared by the 2-D axes and the 3-D scene. Theme-neutral so it
// reads on both light and dark (Plotly can't resolve CSS custom properties).
const AXIS = {
	showgrid: true,
	gridcolor: "rgba(128,128,128,0.2)",
	zeroline: false,
	showticklabels: false,
	showspikes: false,
};

type Props = { runId: string; onFocusCluster?: (clusterId: number) => void };

export default function ProjectionScatter({ runId, onFocusCluster }: Props) {
	const [dims, setDims] = useState<2 | 3>(2);
	const { data, isLoading, isError } = useProjection(runId, dims);

	const axisName = data?.metric === "precomputed" ? "MDS" : "PC";
	// Render from the LOADED data's dimensionality, not the requested `dims` —
	// during a 2D⇄3D toggle the refetch is in flight, so using the requested dims
	// would draw a flat 3-D plane over stale 2-D data for a frame.
	const renderDims = data?.dims ?? dims;

	const traces = useMemo<Plotly.Data[]>(() => {
		if (!data || data.nodes.length === 0) return [];
		const nodes = data.nodes;
		const common = {
			mode: "markers" as const,
			marker: {
				size: data.dims === 3 ? 4 : 7,
				color: nodes.map((n) => nodeColor(n.cluster, n.verdict)),
				line: { width: 0 },
				opacity: 0.85,
			},
			text: nodes.map((n) => n.id),
			customdata: nodes.map((n) => [n.cluster, n.verdict] as [number, string]),
			hovertemplate:
				"%{text}<br>cluster %{customdata[0]} · %{customdata[1]}<extra></extra>",
		};
		const x = nodes.map((n) => n.x);
		const y = nodes.map((n) => n.y);
		if (data.dims === 3) {
			return [
				{ type: "scatter3d", x, y, z: nodes.map((n) => n.z ?? 0), ...common },
			];
		}
		return [{ type: "scatter", x, y, ...common }];
	}, [data]);

	const layout = useMemo<Partial<Plotly.Layout>>(() => {
		// Axis title carries the variance it explains, e.g. "PC1 · 34%".
		const title = (i: number) => {
			const v = data?.axes?.[i]?.variance;
			return `${axisName}${i + 1}${v != null ? ` · ${Math.round(v * 100)}%` : ""}`;
		};
		const base: Partial<Plotly.Layout> = {
			autosize: true,
			margin: { l: 0, r: 0, t: 0, b: 0 },
			paper_bgcolor: "rgba(0,0,0,0)",
			plot_bgcolor: "rgba(0,0,0,0)",
			font: { color: "rgb(139,148,163)" },
			showlegend: false,
			hovermode: "closest",
		};
		if (renderDims === 3) {
			base.scene = {
				xaxis: { ...AXIS, title: { text: title(0) } },
				yaxis: { ...AXIS, title: { text: title(1) } },
				zaxis: { ...AXIS, title: { text: title(2) } },
			};
		} else {
			base.xaxis = { ...AXIS, title: { text: title(0) } };
			base.yaxis = { ...AXIS, title: { text: title(1) } };
		}
		return base;
	}, [renderDims, axisName, data]);

	const onClick = (e: Readonly<Plotly.PlotMouseEvent>) => {
		const pt = e.points?.[0];
		if (!pt) return;
		const cd = pt.customdata as unknown;
		const cluster = Array.isArray(cd) ? Number(cd[0]) : Number(cd);
		if (Number.isFinite(cluster)) onFocusCluster?.(cluster);
	};

	return (
		<div className="space-y-3">
			<div className="flex flex-wrap items-center justify-between gap-3">
				<p className="text-muted-foreground text-sm">
					Each point is a transaction placed by a{" "}
					{axisName === "MDS" ? "MDS" : "PCA"} projection of the features DBSCAN
					clustered on. Coloured by cluster; click a point to focus its cluster.
				</p>
				<div className="border-border inline-flex overflow-hidden rounded-md border">
					{([2, 3] as const).map((d) => (
						<Button
							key={d}
							variant="ghost"
							size="sm"
							className={cn(
								"rounded-none",
								dims === d && "bg-accent text-accent-foreground",
							)}
							aria-pressed={dims === d}
							onClick={() => setDims(d)}
						>
							{d}D
						</Button>
					))}
				</div>
			</div>

			{data?.axes.some((a) => a.top_features.length > 0) && (
				<div className="text-muted-foreground space-y-0.5 text-xs">
					<div>
						What each axis represents (↑/↓ = same/opposite direction;
						orientation is arbitrary):
					</div>
					{data.axes.map((a, i) =>
						a.top_features.length ? (
							<div key={`${axisName}${i}`}>
								<span className="text-foreground font-semibold">
									{axisName}
									{i + 1}
								</span>
								{a.variance != null
									? ` · ${Math.round(a.variance * 100)}%`
									: ""}
								:{" "}
								{a.top_features
									.map(
										(f) =>
											`${f.weight >= 0 ? "↑" : "↓"} ${f.name.replace(/_/g, " ")}`,
									)
									.join(", ")}
							</div>
						) : null,
					)}
				</div>
			)}

			{data && (
				<p className="text-muted-foreground text-xs">
					showing {data.shown.toLocaleString()} of {data.total.toLocaleString()}{" "}
					transactions {data.truncated ? "· (capped)" : ""}
				</p>
			)}

			<div className="border-border relative h-[460px] overflow-hidden rounded-md border">
				{isError ? (
					<p className="text-destructive p-4 text-sm">
						Failed to load the projection.
					</p>
				) : data && data.nodes.length === 0 ? (
					<p className="text-muted-foreground p-4 text-sm">
						No clustered transactions to project.
					</p>
				) : (
					<Plot
						data={traces}
						layout={layout}
						config={{ displayModeBar: false, responsive: true }}
						useResizeHandler
						style={{ width: "100%", height: "100%" }}
						onClick={onClick}
					/>
				)}
				{isLoading && (
					<div className="bg-background/60 text-muted-foreground absolute inset-0 flex items-center justify-center text-sm">
						Projecting…
					</div>
				)}
			</div>
		</div>
	);
}
