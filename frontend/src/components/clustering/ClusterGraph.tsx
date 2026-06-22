/**
 * Cytoscape transaction-cluster graph (ported from the engine's GraphView,
 * reskinned to the TMS theme). Nodes are transactions coloured by cluster /
 * verdict; edges link transactions that share an address (entity co-spend).
 */
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import { useEffect, useMemo, useRef } from "react";

import type { GraphData } from "@/lib/api/clustering";
import { nodeColor } from "./verdict";

cytoscape.use(fcose);

type Props = {
	data: GraphData | null | undefined;
	onNodeTap?: (txHash: string, cluster: number) => void;
	className?: string;
};

export function ClusterGraph({ data, onNodeTap, className }: Props) {
	const containerRef = useRef<HTMLDivElement | null>(null);
	const cyRef = useRef<cytoscape.Core | null>(null);

	const elements = useMemo(() => {
		if (!data) return [];
		const nodes = data.nodes.map((n) => ({
			data: {
				id: n.id,
				cluster: n.cluster,
				color: nodeColor(n.cluster, n.verdict),
			},
		}));
		const edges = data.edges.map((e, i) => ({
			data: { id: `e${i}`, source: e.source, target: e.target },
		}));
		return [...nodes, ...edges];
	}, [data]);

	useEffect(() => {
		const container = containerRef.current;
		if (!container || !data) return;
		// The fcose layout is O(n) but the initial build can jank for a few
		// hundred nodes; the consumer shows a loading state before this runs.
		const cy = cytoscape({
			container,
			elements,
			style: [
				{
					selector: "node",
					style: {
						"background-color": "data(color)",
						width: 12,
						height: 12,
						"border-width": 0,
					},
				},
				{
					selector: "edge",
					style: {
						"line-color": "#94a3b8",
						"line-opacity": 0.35,
						width: 1,
						"curve-style": "haystack",
					},
				},
				{
					selector: "node:selected",
					style: { "border-width": 3, "border-color": "#3b82f6" },
				},
			],
			layout: {
				name: "fcose",
				animate: false,
				nodeRepulsion: 8000,
				idealEdgeLength: 60,
				// fcose-specific keys are untyped in cytoscape's LayoutOptions.
			} as cytoscape.LayoutOptions,
			minZoom: 0.1,
			maxZoom: 3,
		});
		cyRef.current = cy;
		if (onNodeTap) {
			cy.on("tap", "node", (evt) => {
				const n = evt.target;
				onNodeTap(n.id(), n.data("cluster"));
			});
		}
		return () => {
			cy.destroy();
			cyRef.current = null;
		};
	}, [elements, data, onNodeTap]);

	return (
		<div
			ref={containerRef}
			className={className}
			style={{ width: "100%", height: "100%", minHeight: 420 }}
		/>
	);
}
