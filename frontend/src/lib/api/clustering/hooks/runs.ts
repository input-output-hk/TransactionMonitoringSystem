// React Query hooks: cluster runs, cluster summaries/labels, parameter
// evaluation, the cluster graph + feature-space projection, and per-cluster
// transaction drill-down. Public surface (re-exported by the barrel).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type {
	ClusterSummary,
	ClusterVerdict,
	Evaluation,
	FeatureSet,
	GraphData,
	ListPage,
	ProjectionData,
	Run,
	TxRow,
} from "../types";
import {
	arrayOf,
	clusterItem,
	listPage,
	runItem,
	validateClusterTxs,
	validateEvaluation,
	validateGraph,
	validateProjection,
} from "../validation";
import { get, MAX_PAGE_LIMIT, send } from "../transport";

export function useRuns(target: string | undefined) {
	return useQuery({
		queryKey: ["clustering", "runs", target],
		// The endpoint returns a {count,total,data} envelope; fetch one max-size
		// page and unwrap so consumers keep seeing a plain array.
		queryFn: async () =>
			(
				await get<ListPage<Run>>(
					`/runs?target=${encodeURIComponent(target!)}&limit=${MAX_PAGE_LIMIT}`,
					listPage("/runs", runItem),
				)
			).data,
		enabled: !!target,
	});
}

export function useClusterSummary(runId: string | undefined) {
	return useQuery({
		queryKey: ["clustering", "clusters", runId],
		queryFn: () =>
			get<ClusterSummary[]>(
				`/runs/${runId}/clusters`,
				arrayOf("/runs/clusters", clusterItem),
			),
		enabled: !!runId,
	});
}

/**
 * Lazy parameter evaluation for a target + feature set (k-distance curve +
 * DBSCAN grid). `enabled: false` so it never runs on mount; the Tuning panel
 * triggers it with `refetch()`. High staleTime so a result sticks around while
 * the analyst reads it.
 */
export function useEvaluation(
	target: string | undefined,
	featureSet: FeatureSet,
) {
	return useQuery({
		queryKey: ["clustering", "evaluation", target, featureSet],
		queryFn: () =>
			get<Evaluation>(
				`/evaluation?target=${encodeURIComponent(target!)}&feature_set=${featureSet}`,
				validateEvaluation,
			),
		enabled: false,
		staleTime: 5 * 60_000,
	});
}

/** Run a manual DBSCAN pass with explicit parameters. Creates a CUSTOM run that
 *  does not supersede the canonical system run for scoring. */
export function useRunCluster() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (body: {
			target: string;
			feature_set: FeatureSet;
			eps: number;
			min_samples: number;
		}) => send<Run>("POST", "/cluster", body),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}

export function useClusterGraph(runId: string | undefined, limit = 400) {
	return useQuery({
		queryKey: ["clustering", "graph", runId, limit],
		queryFn: () =>
			get<GraphData>(`/runs/${runId}/graph?limit=${limit}`, validateGraph),
		enabled: !!runId,
	});
}

/** The feature-space projection (PCA/MDS) for a run at the requested
 *  dimensionality. The 2-D and 3-D views are the same projection truncated to
 *  2 or 3 components, so dims is part of the query key. */
export function useProjection(
	runId: string | undefined,
	dims: 2 | 3,
	limit = 1500,
) {
	return useQuery({
		queryKey: ["clustering", "projection", runId, dims, limit],
		queryFn: () =>
			get<ProjectionData>(
				`/runs/${runId}/projection?dims=${dims}&limit=${limit}`,
				validateProjection,
			),
		enabled: !!runId,
	});
}

export function useLabelCluster() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (a: {
			runId: string;
			clusterId: number;
			verdict: ClusterVerdict;
		}) =>
			send("POST", `/runs/${a.runId}/clusters/${a.clusterId}/label`, {
				verdict: a.verdict,
			}),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}

export function useClearClusterLabel() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (a: { runId: string; clusterId: number }) =>
			send("POST", `/runs/${a.runId}/clusters/${a.clusterId}/clear-label`, {}),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}

export function useClusterTransactions(
	runId: string | undefined,
	clusterId: number | null,
	limit = 200,
) {
	return useQuery({
		queryKey: ["clustering", "cluster-txs", runId, clusterId, limit],
		queryFn: () =>
			get<{ transactions: TxRow[] }>(
				`/runs/${runId}/clusters/${clusterId}/transactions?limit=${limit}`,
				validateClusterTxs,
			),
		enabled: !!runId && clusterId !== null,
	});
}
