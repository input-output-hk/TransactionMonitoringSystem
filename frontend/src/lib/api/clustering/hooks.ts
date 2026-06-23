// React Query hooks for the clustering client. Public surface (re-exported by
// the barrel) together with the types.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type {
	AnomalyRun,
	AnomalyTopResponse,
	ClusterSummary,
	ClusterVerdict,
	Contract,
	Evaluation,
	FeatureSet,
	GraphData,
	IdentifyResult,
	Job,
	JobStatus,
	LatestInteractionsResponse,
	ProjectionData,
	Run,
	TxRow,
} from "./types";
import {
	arrayOf,
	anomalyRunItem,
	clusterItem,
	jobItem,
	runItem,
	validateAnomalyTop,
	validateClusterTxs,
	validateContracts,
	validateEvaluation,
	validateGraph,
	validateIdentify,
	validateLatest,
	validateProjection,
} from "./validation";
import { get, send } from "./transport";

// --- hooks: watchlist -------------------------------------------------------

const CONTRACTS_KEY = ["clustering", "contracts"] as const;

export function useContracts(pollMs = 10_000) {
	return useQuery({
		queryKey: CONTRACTS_KEY,
		queryFn: () => get<Contract[]>("/contracts", validateContracts),
		refetchInterval: pollMs,
	});
}

/** Upper bound the API accepts for max_txs (MAX_TXS_CAP server-side). */
export const MAX_TXS_CAP = 50_000;

export function useAddContract() {
	const qc = useQueryClient();
	return useMutation({
		// max_txs: how many of the most recent txs to import (omit = the full
		// configured window). reprocess: force a full re-cluster on re-add.
		mutationFn: (body: {
			target: string;
			label?: string;
			max_txs?: number;
			reprocess?: boolean;
		}) => send<{ job_id: string }>("POST", "/contracts", body),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

export function useDeleteContract() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (target: string) =>
			send<{ deleted: boolean }>(
				"DELETE",
				`/contracts/${encodeURIComponent(target)}`,
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

/** Rename a watched contract (its display label). */
export function useRenameContract() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (a: { target: string; label: string }) =>
			send<Contract>("PATCH", `/contracts/${encodeURIComponent(a.target)}`, {
				label: a.label,
			}),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

/** Live, offline identification of a (debounced) typed target. Enabled only on
 *  a non-empty target; identification is deterministic so it caches for a while.
 *  The caller debounces the `target` it passes in. */
export function useIdentify(target: string) {
	const trimmed = target.trim();
	return useQuery({
		queryKey: ["clustering", "identify", trimmed],
		queryFn: () =>
			get<IdentifyResult>(
				`/registry/identify?target=${encodeURIComponent(trimmed)}`,
				validateIdentify,
			),
		enabled: trimmed.length > 0,
		staleTime: 5 * 60_000,
		retry: false,
	});
}

/** Force an immediate incremental re-classify (the auto feed supersedes this;
 *  exposed as a manual "refresh now" for analysts). */
export function useClassifyNow() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (target: string) =>
			send<{ job_id: string }>(
				"POST",
				`/contracts/${encodeURIComponent(target)}/classify-new`,
				{},
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: CONTRACTS_KEY }),
	});
}

// --- hooks: runs / clusters / anomalies / graph -----------------------------

export function useRuns(target: string | undefined) {
	return useQuery({
		queryKey: ["clustering", "runs", target],
		queryFn: () =>
			get<Run[]>(
				`/runs?target=${encodeURIComponent(target!)}`,
				arrayOf("/runs", runItem),
			),
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

export function useAnomalyRuns(target: string | undefined) {
	return useQuery({
		queryKey: ["clustering", "anomaly-runs", target],
		queryFn: () =>
			get<AnomalyRun[]>(
				`/anomaly-runs?target=${encodeURIComponent(target!)}`,
				arrayOf("/anomaly-runs", anomalyRunItem),
			),
		enabled: !!target,
	});
}

export function useTopAnomalies(runId: string | undefined, limit = 100) {
	return useQuery({
		queryKey: ["clustering", "anomaly-top", runId, limit],
		queryFn: () =>
			get<AnomalyTopResponse>(
				`/anomaly-runs/${runId}/top?limit=${limit}`,
				validateAnomalyTop,
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

/** Run a manual anomaly detection pass. Creates a CUSTOM anomaly run. */
export function useDetectAnomaly() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (body: { target: string; feature_set: FeatureSet }) =>
			send<AnomalyRun>("POST", "/anomaly", body),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}

/** Delete a custom anomaly run and its scores. System runs are guarded
 *  server-side; the UI only offers this for custom runs. */
export function useDeleteAnomalyRun() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (runId: string) =>
			send<{ deleted: boolean }>(
				"DELETE",
				`/anomaly-runs/${encodeURIComponent(runId)}`,
			),
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

// --- hooks: jobs ------------------------------------------------------------

/** Terminal job statuses — a job in either state is finished and its actions can
 *  safely re-run. Shared by the poll-stop logic and the stage presentation. */
const TERMINAL_JOB_STATUS: ReadonlySet<JobStatus> = new Set<JobStatus>([
	"done",
	"failed",
]);

export function isTerminalJob(status: JobStatus): boolean {
	return TERMINAL_JOB_STATUS.has(status);
}

/** Poll a single job while it is running; stop polling once it is terminal. */
export function useJob(jobId: string | undefined, pollMs = 2500) {
	return useQuery({
		queryKey: ["clustering", "job", jobId],
		queryFn: () => get<Job>(`/jobs/${jobId}`, jobItem),
		enabled: !!jobId,
		// `query.state.data` is the last fetched job; keep polling until terminal.
		refetchInterval: (query) =>
			query.state.data && isTerminalJob(query.state.data.status)
				? false
				: pollMs,
	});
}

/** All known jobs (newest first), polled so card status badges stay live. */
export function useJobs(pollMs = 2500) {
	return useQuery({
		queryKey: ["clustering", "jobs"],
		queryFn: () => get<Job[]>("/jobs", arrayOf("/jobs", jobItem)),
		refetchInterval: pollMs,
	});
}

// --- hooks: cluster drill-down + per-tx labels ------------------------------

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

/** The latest N stored interactions with a validator, newest first. The detail
 *  page gates the fetch by mounting this only on the active tab (Radix unmounts
 *  inactive panels), so no explicit enable flag is needed. */
export function useLatest(target: string | undefined, limit = 100) {
	return useQuery({
		queryKey: ["clustering", "latest", target, limit],
		queryFn: () =>
			get<LatestInteractionsResponse>(
				`/contracts/${encodeURIComponent(target!)}/latest?limit=${limit}`,
				validateLatest,
			),
		enabled: !!target,
	});
}

/** Apply a manual verdict to a single transaction (overrides cluster
 *  inheritance for this tx only; does not propagate to future txs). */
export function useLabelTx() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (a: {
			target: string;
			txHash: string;
			verdict: ClusterVerdict;
		}) =>
			send(
				"POST",
				`/contracts/${encodeURIComponent(a.target)}/transactions/${encodeURIComponent(a.txHash)}/label`,
				{ verdict: a.verdict },
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}

export function useClearTxLabel() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: (a: { target: string; txHash: string }) =>
			send(
				"POST",
				`/contracts/${encodeURIComponent(a.target)}/transactions/${encodeURIComponent(a.txHash)}/clear-label`,
				{},
			),
		onSuccess: () => qc.invalidateQueries({ queryKey: ["clustering"] }),
	});
}
