// Runtime validation for the clustering responses.
//
// zod is not a frontend dependency, so rather than add one we run pragmatic
// hand-rolled guards on the response shapes whose fields are consumed with
// methods that throw on `undefined` (`.toFixed`, `.toLocaleString`, `.map`,
// arithmetic). A malformed/HTML-error-page body that slips past `res.ok`
// would otherwise blow up deep in render with an opaque "cannot read
// properties of undefined"; failing loud here lets the query surface an
// error state instead. A `Validator<T>` returns the value (typed) or throws.
//
// Internal to the clustering client: these are not part of the public API
// (the barrel re-exports only types + hooks).
import type {
	AnomalyCandidate,
	AnomalyRun,
	AnomalyTopResponse,
	ClusteringConfig,
	ClusterSummary,
	Contract,
	Evaluation,
	FeatureSet,
	GraphData,
	GraphNode,
	IdentifyResult,
	Job,
	LatestInteraction,
	LatestInteractionsResponse,
	ProjectionData,
	ProjectionNode,
	Run,
	TxRow,
} from "./types";

export type Validator<T> = (raw: unknown) => T;

export class ResponseShapeError extends Error {
	constructor(path: string, detail: string) {
		super(`clustering ${path}: malformed response (${detail})`);
		this.name = "ResponseShapeError";
	}
}

export function isObject(v: unknown): v is Record<string, unknown> {
	return typeof v === "object" && v !== null;
}

/** Build a validator that requires `obj` to be an object carrying every key in
 *  `numberFields` as a finite number and every key in `stringFields` as a
 *  string. Returns the value cast to T. Other fields pass through unchecked. */
export function shapeValidator<T>(
	path: string,
	numberFields: readonly string[],
	stringFields: readonly string[],
): Validator<T> {
	return (raw) => {
		if (!isObject(raw))
			throw new ResponseShapeError(path, "expected an object");
		for (const f of numberFields) {
			if (typeof raw[f] !== "number" || !Number.isFinite(raw[f])) {
				throw new ResponseShapeError(
					path,
					`field "${f}" is not a finite number`,
				);
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
export function arrayOf<T>(path: string, item: Validator<T>): Validator<T[]> {
	return (raw) => {
		if (!Array.isArray(raw))
			throw new ResponseShapeError(path, "expected an array");
		return raw.map(item);
	};
}

// Only the fields actually read with throwing methods are required; the rest of
// each type is structural and tolerated as optional to stay forgiving.
export const validateContracts = arrayOf<Contract>(
	"/contracts",
	shapeValidator(
		"/contracts",
		["tx_count", "drift_score"],
		["target", "status"],
	),
);

/** `host_backed` gates the onboarding form's behaviour, so validate it as a
 *  boolean rather than letting a missing/garbled field fall through to falsy.
 *  `window_txs` is rendered with `.toLocaleString()`, so it must be a number. */
export const validateConfig: Validator<ClusteringConfig> = (raw) => {
	if (!isObject(raw)) throw new ResponseShapeError("/config", "expected an object");
	if (typeof raw.host_backed !== "boolean")
		throw new ResponseShapeError("/config", 'field "host_backed" is not a boolean');
	if (typeof raw.window_txs !== "number" || !Number.isFinite(raw.window_txs))
		throw new ResponseShapeError("/config", 'field "window_txs" is not a finite number');
	return raw as ClusteringConfig;
};

export const runItem = shapeValidator<Run>(
	"/runs",
	["n_points", "n_clusters", "n_noise"],
	["run_id"],
);

export const clusterItem = shapeValidator<ClusterSummary>(
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

export const anomalyRunItem = shapeValidator<AnomalyRun>(
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
		// Rendered with throwing methods (`.toFixed`) below; iso_score is nullable
		// so it stays unchecked and is guarded at the call site.
		"lof_score",
		"fees",
		"input_count",
		"output_count",
		"distinct_assets",
	],
	["tx_hash"],
);

export const validateIdentify: Validator<IdentifyResult> = (raw) => {
	const path = "/registry/identify";
	if (!isObject(raw)) throw new ResponseShapeError(path, "expected an object");
	// `label` is read with `.trim()` etc. in the form; default missing to "".
	return {
		valid: !!raw.valid,
		target_type: typeof raw.target_type === "string" ? raw.target_type : null,
		script_hash: typeof raw.script_hash === "string" ? raw.script_hash : null,
		label: typeof raw.label === "string" ? raw.label : "",
	};
};

export const jobItem = shapeValidator<Job>(
	"/jobs",
	[],
	["job_id", "target", "status", "kind"],
);

const txRowItem = shapeValidator<TxRow>(
	"/runs/clusters/transactions",
	["fees", "input_count", "output_count", "distinct_assets", "redeemer_count"],
	["tx_hash"],
);

export const validateClusterTxs: Validator<{ transactions: TxRow[] }> = (
	raw,
) => {
	const path = "/runs/clusters/transactions";
	if (!isObject(raw)) throw new ResponseShapeError(path, "expected an object");
	return { transactions: arrayOf(path, txRowItem)(raw.transactions) };
};

const latestItem = shapeValidator<LatestInteraction>(
	"/contracts/latest",
	[
		"fees",
		"total_output_lovelace",
		"input_count",
		"output_count",
		"distinct_assets",
	],
	["tx_hash"],
);

export const validateLatest: Validator<LatestInteractionsResponse> = (raw) => {
	const path = "/contracts/latest";
	if (!isObject(raw)) throw new ResponseShapeError(path, "expected an object");
	return {
		target: typeof raw.target === "string" ? raw.target : "",
		feature_set: raw.feature_set as FeatureSet,
		transactions: arrayOf(path, latestItem)(raw.transactions),
	};
};

const graphNodeItem = shapeValidator<GraphNode>(
	"/runs/graph (node)",
	["cluster"],
	["id"],
);

export const validateGraph: Validator<GraphData> = (raw) => {
	const path = "/runs/graph";
	const obj = shapeValidator<GraphData>(
		path,
		["total", "shown"],
		["run_id"],
	)(raw);
	arrayOf(
		path,
		graphNodeItem,
	)((obj as unknown as Record<string, unknown>).nodes);
	if (!Array.isArray((obj as unknown as Record<string, unknown>).edges)) {
		throw new ResponseShapeError(path, 'field "edges" is not an array');
	}
	return obj;
};

const projectionNodeItem = shapeValidator<ProjectionNode>(
	"/runs/projection (node)",
	["cluster", "x", "y"],
	["id"],
);

export const validateProjection: Validator<ProjectionData> = (raw) => {
	const path = "/runs/projection";
	const obj = shapeValidator<ProjectionData>(
		path,
		["dims", "total", "shown"],
		["run_id"],
	)(raw);
	const o = obj as unknown as Record<string, unknown>;
	arrayOf(path, projectionNodeItem)(o.nodes);
	if (!Array.isArray(o.axes)) {
		throw new ResponseShapeError(path, 'field "axes" is not an array');
	}
	return obj;
};

export const validateEvaluation: Validator<Evaluation> = (raw) => {
	const path = "/evaluation";
	const obj = shapeValidator<Evaluation>(path, ["n_points"], [])(raw);
	const o = obj as unknown as Record<string, unknown>;
	// A "message-only" evaluation (e.g. too few points) omits the grid/curve; the
	// panel renders the message instead, so only validate the heavy shapes when
	// they are present.
	if (typeof o.message !== "string") {
		if (!isObject(o.k_distance) || !Array.isArray(o.k_distance.distances)) {
			throw new ResponseShapeError(path, "missing k_distance.distances array");
		}
		if (!Array.isArray(o.grid)) {
			throw new ResponseShapeError(path, 'field "grid" is not an array');
		}
	}
	return obj;
};

export const validateAnomalyTop: Validator<AnomalyTopResponse> = (raw) => {
	const path = "/anomaly-runs/top";
	if (!isObject(raw)) throw new ResponseShapeError(path, "expected an object");
	return { candidates: arrayOf(path, candidateItem)(raw.candidates) };
};
