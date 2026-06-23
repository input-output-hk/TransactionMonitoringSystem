/**
 * Client for the clustering module, reached through the `/api/clustering`
 * reverse-proxy (session-authed, same-origin). Powers the Validators surfaces:
 * the watched-contract registry, cluster summaries, anomaly tables, and the
 * cluster graph.
 *
 * The clustering module auto-feeds and auto-fits each watched contract as the
 * chain is ingested, so this client is read + manage only: add/remove a watched
 * contract and label clusters/transactions. Onboarding/refit happen
 * automatically, so there are no manual download/evaluate/cluster controls.
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

// --- runtime validation -----------------------------------------------------
//
// zod is not a frontend dependency, so rather than add one we run pragmatic
// hand-rolled guards on the response shapes whose fields are consumed with
// methods that throw on `undefined` (`.toFixed`, `.toLocaleString`, `.map`,
// arithmetic). A malformed/HTML-error-page body that slips past `res.ok`
// would otherwise blow up deep in render with an opaque "cannot read
// properties of undefined"; failing loud here lets the query surface an
// error state instead. A `Validator<T>` returns the value (typed) or throws.

type Validator<T> = (raw: unknown) => T;

class ResponseShapeError extends Error {
	constructor(path: string, detail: string) {
		super(`clustering ${path}: malformed response (${detail})`);
		this.name = "ResponseShapeError";
	}
}

function isObject(v: unknown): v is Record<string, unknown> {
	return typeof v === "object" && v !== null;
}

/** Build a validator that requires `obj` to be an object carrying every key in
 *  `numberFields` as a finite number and every key in `stringFields` as a
 *  string. Returns the value cast to T. Other fields pass through unchecked. */
function shapeValidator<T>(
	path: string,
	numberFields: readonly string[],
	stringFields: readonly string[],
): Validator<T> {
	return (raw) => {
		if (!isObject(raw)) throw new ResponseShapeError(path, "expected an object");
		for (const f of numberFields) {
			if (typeof raw[f] !== "number" || !Number.isFinite(raw[f])) {
				throw new ResponseShapeError(path, `field "${f}" is not a finite number`);
			}
		}
		for (const f of stringFields) {
			if (typeof raw[f] !== "string") {
				throw new ResponseShapeError(path, `field "${f}" is not a string`);
			}
		}
		return raw as T;
	};
}

/** Wrap an item validator so it applies to every element of an array body. */
function arrayOf<T>(path: string, item: Validator<T>): Validator<T[]> {
	return (raw) => {
		if (!Array.isArray(raw)) throw new ResponseShapeError(path, "expected an array");
		return raw.map(item);
	};
}

// Only the fields actually read with throwing methods are required; the rest of
// each type is structural and tolerated as optional to stay forgiving.
const validateContracts = arrayOf<Contract>(
	"/contracts",
	shapeValidator(
		"/contracts",
		["tx_count", "drift_score"],
		["target", "status"],
	),
);

const runItem = shapeValidator<Run>(
	"/runs",
	["n_points", "n_clusters", "n_noise"],
	["run_id"],
);

const clusterItem = shapeValidator<ClusterSummary>(
	"/runs/clusters",
	[
		"cluster_id",
		"size",
		"avg_fees",
		"avg_output_lovelace",
		"avg_inputs",
		"avg_outputs",
		"anomaly_count",
	],
	[],
);

const anomalyRunItem = shapeValidator<AnomalyRun>(
	"/anomaly-runs",
	["n_points", "n_flagged"],
	["run_id"],
);

const candidateItem = shapeValidator<AnomalyCandidate>(
	"/anomaly-runs/top",
	[
		"score_rank",
		"consensus",
		"votes",
		"input_count",
		"output_count",
		"distinct_assets",
	],
	["tx_hash"],
);

const graphNodeItem = shapeValidator<GraphNode>(
	"/runs/graph (node)",
	["cluster"],
	["id"],
);

const validateGraph: Validator<GraphData> = (raw) => {
	const path = "/runs/graph";
	const obj = shapeValidator<GraphData>(path, ["total", "shown"], ["run_id"])(raw);
	arrayOf(path, graphNodeItem)((obj as unknown as Record<string, unknown>).nodes);
	if (!Array.isArray((obj as unknown as Record<string, unknown>).edges)) {
		throw new ResponseShapeError(path, 'field "edges" is not an array');
	}
	return obj;
};

const validateAnomalyTop: Validator<AnomalyTopResponse> = (raw) => {
	const path = "/anomaly-runs/top";
	if (!isObject(raw)) throw new ResponseShapeError(path, "expected an object");
	return { candidates: arrayOf(path, candidateItem)(raw.candidates) };
};

// --- transport --------------------------------------------------------------

async function get<T>(path: string, validate?: Validator<T>): Promise<T> {
	const res = await fetchWithAuth(`${BASE}${path}`);
	if (!res.ok) throw new Error(`clustering ${path} failed: ${res.status}`);
	const raw = await res.json();
	return validate ? validate(raw) : (raw as T);
}

async function send<T>(
	method: string,
	path: string,
	body?: unknown,
	validate?: Validator<T>,
): Promise<T> {
	const res = await fetchWithAuth(`${BASE}${path}`, {
		method,
		headers: { "Content-Type": "application/json" },
		body: body === undefined ? undefined : JSON.stringify(body),
	});
	if (!res.ok) throw new Error(`clustering ${method} ${path} failed: ${res.status}`);
	const raw = await res.json();
	return validate ? validate(raw) : (raw as T);
}

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

export type AnomalyTopResponse = { candidates: AnomalyCandidate[] };

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

export function useClusterGraph(runId: string | undefined, limit = 400) {
	return useQuery({
		queryKey: ["clustering", "graph", runId, limit],
		queryFn: () =>
			get<GraphData>(`/runs/${runId}/graph?limit=${limit}`, validateGraph),
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
