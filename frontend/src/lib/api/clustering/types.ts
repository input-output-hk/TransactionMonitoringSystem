/** Shared types for the clustering client (ported from the engine's
 *  ui/src/types.ts). Pure type/data declarations, no runtime dependencies. */

/** Envelope returned by every clustering collection endpoint (mirrors the host
 *  API's ListResponse): `data` is the requested page, `count` its length, and
 *  `total` the full filtered collection size. */
export type ListPage<T> = { count: number; total: number; data: T[] };

export type Verdict = "malicious" | "benign" | "anomaly" | "normal";
export type ClusterVerdict = "malicious" | "benign";
export type FeatureSet = "shape" | "graph" | "combined";

/** The selectable feature sets, in display order. Single source for the pickers. */
export const FEATURE_SETS: FeatureSet[] = ["shape", "graph", "combined"];

/** Read-only deployment config (`GET /config`). When `host_backed`, the engine
 *  reads txs from the host tables and fits over the rolling window `window_txs`,
 *  so the onboarding form hides the (no-op) per-contract "max txs" control —
 *  unless `history_source` is set (a secondary pre-deployment history source),
 *  which re-purposes "max txs" as the per-contract history depth. Absent on
 *  not-yet-upgraded sidecars; treated as "" (disabled). */
export type ClusteringConfig = {
	host_backed: boolean;
	window_txs: number;
	history_source?: string;
};

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
	/** Locally-backfilled pre-deployment rows (detail endpoint only; 0 in lists). */
	history_tx_count?: number;
	/** "none" | "in_progress" | "complete" (detail endpoint only). */
	history_status?: string;
};

export type RunOrigin = "system" | "custom";

export type Run = {
	run_id: string;
	target: string;
	feature_set: FeatureSet;
	eps: number;
	min_samples: number;
	metric: string;
	n_points: number;
	n_clusters: number;
	n_noise: number;
	silhouette: number | null;
	origin: RunOrigin;
	created_at: string;
};

export type GridRow = {
	eps: number;
	min_samples: number;
	n_clusters: number;
	n_noise: number;
	noise_ratio: number;
	silhouette: number | null;
};

export type Evaluation = {
	feature_set: string;
	metric: string;
	n_points: number;
	n_features: number | null;
	k_distance: { k: number; distances: number[]; knee_eps: number | null };
	grid: GridRow[];
	recommended: { eps: number; min_samples: number; rationale: string } | null;
	message?: string;
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
	// Comma-separated detector names (e.g. "iso,lof,dbscan").
	methods: string;
	n_points: number;
	n_flagged: number;
	eps: number;
	min_samples: number;
	origin: RunOrigin;
	created_at: string;
};

/** One human-readable driver of an anomaly verdict (a top deviating shape
 *  feature). Present only on shape-run candidates; absent on graph runs. */
export type AnomalyReason = {
	label: string; // "inputs", "output value", "fee", "time of day", …
	direction: string; // "high" | "low" | "unusual" | "combo"
	detail: string; // "far above typical", "unusual time of day", …
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
	block_time: string;
	fees: number;
	size: number;
	total_input_lovelace: number;
	total_output_lovelace: number;
	net_lovelace: number;
	input_count: number;
	output_count: number;
	distinct_assets: number;
	redeemer_count: number;
	hour_of_day: number;
	day_of_week: number;
	// Why it was flagged (top deviating features). Present only on shape runs.
	reasons?: AnomalyReason[];
};

export type AnomalyTopResponse = { candidates: AnomalyCandidate[] };

/** Offline identification of a typed target against the bundled registry, used
 *  to prefill the onboarding form. `label` is the registry name (empty if not
 *  recognised). */
export type IdentifyResult = {
	valid: boolean;
	target_type: string | null;
	script_hash: string | null;
	label: string;
};

export type JobStatus =
	| "queued"
	| "checking"
	| "downloading"
	| "clustering"
	| "scoring"
	| "done"
	| "failed";

/** A background onboarding/classify job. `kind` is "onboard" | "classify";
 *  `stage_detail` is a free-text sub-status, `txs_done` the running count. */
export type Job = {
	job_id: string;
	target: string;
	target_type: string;
	max_txs: number;
	reprocess: number;
	kind: string;
	status: JobStatus;
	stage_detail: string;
	txs_done: number;
	error: string;
	created_at: string;
	updated_at: string;
};

/** One transaction inside a cluster's drill-down. `label` is the tx's OWN
 *  explicit label (null = none); `verdict` is the effective (possibly
 *  inherited) call. */
export type TxRow = {
	tx_hash: string;
	block_time: string;
	fees: number;
	total_output_lovelace: number;
	input_count: number;
	output_count: number;
	distinct_assets: number;
	redeemer_count: number;
	verdict: Verdict;
	label: ClusterVerdict | null;
	votes: number;
};

/**
 * One of the latest interactions with a validator (recency feed). `verdict` is
 * computed live against the cluster's CURRENT label state. A tx in no run that
 * hasn't been online-scored yet is `classified: false`, with verdict / cluster
 * / votes unknown (null) — the UI shows "unclassified".
 */
export type LatestInteraction = {
	tx_hash: string;
	block_time: string;
	fees: number;
	size: number;
	total_input_lovelace: number;
	total_output_lovelace: number;
	net_lovelace: number;
	input_count: number;
	output_count: number;
	distinct_assets: number;
	redeemer_count: number;
	classified: boolean;
	verdict: Verdict | null;
	label: ClusterVerdict | null;
	cluster_id: number | null;
	votes: number;
	reasons?: AnomalyReason[];
};

export type LatestInteractionsResponse = {
	target: string;
	feature_set: FeatureSet;
	transactions: LatestInteraction[];
};

// A point in the feature-space projection (PCA for euclidean feature sets, MDS
// for the precomputed graph metric). `z` is present only for a 3-D projection.
export type ProjectionNode = {
	id: string;
	cluster: number;
	verdict: Verdict;
	x: number;
	y: number;
	z?: number | null;
};

export type ProjectionAxisFeature = { name: string; weight: number };

// What one projected axis represents. For PCA axes, `variance` is the fraction
// of total variance explained and `top_features` the highest-|loading| features
// (signed). MDS axes (graph metric) have neither.
export type ProjectionAxis = {
	variance?: number | null;
	top_features: ProjectionAxisFeature[];
};

export type ProjectionData = {
	run_id: string;
	feature_set: FeatureSet;
	dims: number;
	metric: string;
	axes: ProjectionAxis[];
	nodes: ProjectionNode[];
	total: number;
	shown: number;
	truncated: boolean;
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
