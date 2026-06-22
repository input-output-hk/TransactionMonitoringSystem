/**
 * Client for the clustering sidecar, reached through the host's
 * `/api/clustering` reverse-proxy (which forwards to the sidecar's `/api/v1`,
 * session-authed and same-origin). Powers the Validators surfaces: the watched-
 * contract registry, cluster summaries, anomaly tables, and the cluster graph.
 *
 * The integrated deployment auto-feeds and auto-fits each watched contract (the
 * sidecar's scheduler), so this client is read + manage only: add/remove a
 * watched contract and label clusters/transactions. It does not expose the
 * standalone engine's manual download/evaluate/cluster controls.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchWithAuth } from "./fetch";

const BASE = "/api/clustering";

// --- types (ported from the engine's ui/src/types.ts) ----------------------

export type Verdict = "malicious" | "benign" | "anomaly" | "normal";
export type ClusterVerdict = "malicious" | "benign";
export type FeatureSet = "shape" | "graph" | "combined";

export type Contract = {
	target: string;
	target_type: string;
	label: string;
	is_script: number;
	status: string;
	tx_count: number;
	drift_score: number;
	reclustering_suggested: boolean;
	updated_at: string;
};

export type Run = {
	run_id: string;
	target: string;
	feature_set: FeatureSet;
	n_points: number;
	n_clusters: number;
	n_noise: number;
	silhouette: number | null;
	origin: "system" | "custom";
	created_at: string;
};

export type ClusterSummary = {
	cluster_id: number;
	size: number;
	avg_fees: number;
	avg_output_lovelace: number;
	avg_inputs: number;
	avg_outputs: number;
	avg_assets: number;
	verdict: ClusterVerdict | null;
	verdict_conflict: boolean;
	labeled_count: number;
	anomaly_count: number;
};

export type AnomalyRun = {
	run_id: string;
	target: string;
	feature_set: FeatureSet;
	n_points: number;
	n_flagged: number;
	created_at: string;
};

export type AnomalyCandidate = {
	score_rank: number;
	tx_hash: string;
	consensus: number;
	votes: number;
	iso_score: number | null;
	lof_score: number;
	dbscan_noise: number;
	verdict: Verdict;
	label: ClusterVerdict | null;
	fees: number;
	input_count: number;
	output_count: number;
	distinct_assets: number;
	redeemer_count: number;
};

export type GraphNode = { id: string; cluster: number; verdict: Verdict };
export type GraphEdge = { source: string; target: string; weight: number };
export type GraphData = {
	run_id: string;
	nodes: GraphNode[];
	edges: GraphEdge[];
	total: number;
	shown: number;
	truncated: boolean;
};

// --- transport --------------------------------------------------------------

async function get<T>(path: string): Promise<T> {
	const res = await fetchWithAuth(`${BASE}${path}`);
	if (!res.ok) throw new Error(`clustering ${path} failed: ${res.status}`);
	return (await res.json()) as T;
}

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
	const res = await fetchWithAuth(`${BASE}${path}`, {
		method,
		headers: { "Content-Type": "application/json" },
		body: body === undefined ? undefined : JSON.stringify(body),
	});
	if (!res.ok) throw new Error(`clustering ${method} ${path} failed: ${res.status}`);
	return (await res.json()) as T;
}

// --- hooks: watchlist -------------------------------------------------------

const CONTRACTS_KEY = ["clustering", "contracts"] as const;

export function useContracts(pollMs = 10_000) {
	return useQuery({
		queryKey: CONTRACTS_KEY,
		queryFn: () => get<Contract[]>("/contracts"),
		refetchInterval: pollMs,
	});
}

export function useAddContract() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (body: { target: string; label?: string }) =>
			send<{ job_id: string }>("POST", "/contracts", body),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

export function useDeleteContract() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (target: string) =>
			send<{ deleted: boolean }>("DELETE", `/contracts/${encodeURIComponent(target)}`),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

/** Force an immediate incremental re-classify (the auto feed supersedes this;
 *  exposed as a manual "refresh now" for analysts). */
export function useClassifyNow() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (target: string) =>
			send<{ job_id: string }>(
				"POST", `/contracts/${encodeURIComponent(target)}/classify-new`, {},
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

// --- hooks: runs / clusters / anomalies / graph -----------------------------

export function useRuns(target: string | undefined) {
	return useQuery({
		queryKey: ["clustering", "runs", target],
		queryFn: () => get<Run[]>(`/runs?target=${encodeURIComponent(target!)}`),
		enabled: !!target,
	});
}

export function useClusterSummary(runId: string | undefined) {
	return useQuery({
		queryKey: ["clustering", "clusters", runId],
		queryFn: () => get<ClusterSummary[]>(`/runs/${runId}/clusters`),
		enabled: !!runId,
	});
}

export function useAnomalyRuns(target: string | undefined) {
	return useQuery({
		queryKey: ["clustering", "anomaly-runs", target],
		queryFn: () => get<AnomalyRun[]>(`/anomaly-runs?target=${encodeURIComponent(target!)}`),
		enabled: !!target,
	});
}

export type AnomalyTopResponse = { candidates: AnomalyCandidate[] };

export function useTopAnomalies(runId: string | undefined, limit = 100) {
	return useQuery({
		queryKey: ["clustering", "anomaly-top", runId, limit],
		queryFn: () => get<AnomalyTopResponse>(`/anomaly-runs/${runId}/top?limit=${limit}`),
		enabled: !!runId,
	});
}

export function useClusterGraph(runId: string | undefined, limit = 400) {
	return useQuery({
		queryKey: ["clustering", "graph", runId, limit],
		queryFn: () => get<GraphData>(`/runs/${runId}/graph?limit=${limit}`),
		enabled: !!runId,
	});
}

export function useLabelCluster() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (a: { runId: string; clusterId: number; verdict: ClusterVerdict }) =>
			send("POST", `/runs/${a.runId}/clusters/${a.clusterId}/label`, { verdict: a.verdict }),
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
