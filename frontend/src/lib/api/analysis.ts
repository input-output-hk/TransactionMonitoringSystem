import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
	ATTACK_TYPES,
	type AttackType,
	type RiskAlert,
	type Severity,
} from "@/mocks/attacks";
import { formatAnalyzedAt } from "@/lib/utils/dates";
import { shortHash } from "@/lib/utils/strings";
import { fetchWithAuth, getNetwork } from "./fetch";

/* ---------- Backend types (mirrors OpenAPI) ---------- */

type ApiRiskBand = "Low" | "Moderate" | "High" | "Critical";

type ApiAnalysisResult = {
	tx_hash: string;
	network: string;
	scores: Record<string, number>;
	max_score: number;
	max_class: string;
	risk_band: ApiRiskBand;
	// Backend mixes 0..1 normalized scores with raw counts and string IDs.
	sub_scores: Record<string, Record<string, number | string>>;
	analysis_version: string;
	analyzed_at: string; // ISO datetime
	fee: number | null; // lovelace
	output_count: number | null;
};

type ApiAnalysisResults = {
	count: number;
	total: number;
	data: ApiAnalysisResult[];
};

export type RiskAlertsPage = {
	rows: RiskAlert[];
	total: number;
};

/* ---------- Mapping helpers ---------- */

const RISK_BAND_TO_SEVERITY: Record<ApiRiskBand, Severity> = {
	Low: "LOW",
	Moderate: "MEDIUM",
	High: "HIGH",
	Critical: "CRITICAL",
};

const SEVERITY_TO_RISK_BAND: Record<Severity, ApiRiskBand> = {
	LOW: "Low",
	MEDIUM: "Moderate",
	HIGH: "High",
	CRITICAL: "Critical",
};

// Backend uses snake_case attack class names; map to our Title Case AttackType.
const ATTACK_CLASS_BY_SNAKE: Record<string, AttackType> = Object.fromEntries(
	ATTACK_TYPES.map((t) => [t.toLowerCase().replace(/\s+/g, "_"), t]),
) as Record<string, AttackType>;

const SNAKE_BY_ATTACK_TYPE: Record<AttackType, string> = Object.fromEntries(
	ATTACK_TYPES.map((t) => [t, t.toLowerCase().replace(/\s+/g, "_")]),
) as Record<AttackType, string>;

const LOVELACE_PER_ADA = 1_000_000;

function toRiskAlert(r: ApiAnalysisResult): RiskAlert | null {
	const attackType = ATTACK_CLASS_BY_SNAKE[r.max_class];
	if (!attackType) return null;
	// Keep only numeric dimensions — backend mixes 0..1 normalized scores with
	// raw counts and string IDs (e.g. sandwich.pool_id) under sub_scores.
	const rawSub = r.sub_scores?.[r.max_class] ?? {};
	const subScores: Record<string, number> = {};
	for (const [k, v] of Object.entries(rawSub)) {
		if (typeof v === "number" && Number.isFinite(v)) subScores[k] = v;
	}
	return {
		slug: r.tx_hash,
		id: shortHash(r.tx_hash),
		fullHash: r.tx_hash,
		date: formatAnalyzedAt(r.analyzed_at),
		attackType,
		severity: RISK_BAND_TO_SEVERITY[r.risk_band] ?? "LOW",
		riskScore: Math.round(r.max_score),
		feeAda: r.fee !== null ? r.fee / LOVELACE_PER_ADA : 0,
		outputs: r.output_count ?? 0,
		subScores,
	};
}

/* ---------- Fetcher ---------- */

export type RiskAlertsParams = {
	page: number;
	pageSize: number;
	attackType?: AttackType;
	severity?: Severity;
	sort?: "score" | "date";
	/** Inclusive lower bound on `analyzed_at` (ISO datetime). */
	analyzedFrom?: string;
	/** Exclusive upper bound on `analyzed_at` (ISO datetime). */
	analyzedTo?: string;
};

/** Shared query-string builder for the list endpoint. */
function buildResultsQuery(
	p: Omit<RiskAlertsParams, "page" | "pageSize"> & {
		limit: number;
		offset: number;
	},
): URLSearchParams {
	const qs = new URLSearchParams();
	qs.set("network", getNetwork());
	qs.set("limit", String(p.limit));
	qs.set("offset", String(p.offset));
	qs.set("sort", p.sort ?? "date");
	// Only surface transactions that actually triggered an attack class.
	// Without this, the backend includes scored-but-clean transactions
	// (max_class="", max_score=0) which aren't really "alerts".
	qs.set("min_score", "1");
	if (p.attackType) qs.set("attack_class", SNAKE_BY_ATTACK_TYPE[p.attackType]);
	if (p.severity) qs.set("risk_band", SEVERITY_TO_RISK_BAND[p.severity]);
	if (p.analyzedFrom) qs.set("analyzed_from", p.analyzedFrom);
	if (p.analyzedTo) qs.set("analyzed_to", p.analyzedTo);
	return qs;
}

async function fetchRiskAlertsPage(
	p: RiskAlertsParams,
): Promise<RiskAlertsPage> {
	const qs = buildResultsQuery({
		...p,
		limit: p.pageSize,
		offset: p.page * p.pageSize,
	});
	const res = await fetchWithAuth(`/api/analysis/results?${qs.toString()}`);
	if (!res.ok) {
		throw new Error(`Analysis results request failed: ${res.status}`);
	}
	const json = (await res.json()) as ApiAnalysisResults;
	return {
		rows: json.data.map(toRiskAlert).filter((a): a is RiskAlert => a !== null),
		total: json.total,
	};
}

/* ---------- CSV export ---------- */

export type ExportParams = Omit<RiskAlertsParams, "page" | "pageSize">;

/** Plain-shape record used as a CSV row (one column per key). */
export type AlertCsvRow = {
	tx_hash: string;
	analyzed_at: string;
	network: string;
	max_class: string;
	max_score: number;
	risk_band: ApiRiskBand;
	fee_ada: number | "";
	output_count: number | "";
	score_token_dust: number;
	score_large_value: number;
	score_large_datum: number;
	score_multiple_sat: number;
	score_front_running: number;
	score_sandwich: number;
	score_circular: number;
	score_fake_token: number;
	score_phishing: number;
	sub_scores: string; // JSON-stringified sub-scores for the winning class
	analysis_version: string;
};

function toCsvRow(r: ApiAnalysisResult): AlertCsvRow {
	return {
		tx_hash: r.tx_hash,
		analyzed_at: r.analyzed_at,
		network: r.network,
		max_class: r.max_class,
		max_score: r.max_score,
		risk_band: r.risk_band,
		fee_ada: r.fee !== null ? r.fee / LOVELACE_PER_ADA : "",
		output_count: r.output_count ?? "",
		score_token_dust: r.scores.token_dust,
		score_large_value: r.scores.large_value,
		score_large_datum: r.scores.large_datum,
		score_multiple_sat: r.scores.multiple_sat,
		score_front_running: r.scores.front_running,
		score_sandwich: r.scores.sandwich,
		score_circular: r.scores.circular,
		score_fake_token: r.scores.fake_token,
		score_phishing: r.scores.phishing,
		sub_scores: JSON.stringify(r.sub_scores?.[r.max_class] ?? {}),
		analysis_version: r.analysis_version,
	};
}

/**
 * Fetch all alerts matching `params` by paginating through the API.
 *
 * - Uses the backend's max `limit=1000` per request.
 * - Stops at `hardCap` rows (default 50k) to avoid runaway exports.
 * - Calls `onProgress(fetched, total)` after each page so callers can show
 *   a progress UI.
 */
export async function fetchAlertsForExport(
	params: ExportParams,
	options?: {
		hardCap?: number;
		onProgress?: (fetched: number, total: number) => void;
	},
): Promise<AlertCsvRow[]> {
	const hardCap = options?.hardCap ?? 50_000;
	const pageSize = 1000;
	const all: AlertCsvRow[] = [];
	let offset = 0;
	let total = Infinity;

	while (offset < total && all.length < hardCap) {
		const qs = buildResultsQuery({ ...params, limit: pageSize, offset });
		const res = await fetchWithAuth(`/api/analysis/results?${qs.toString()}`);
		if (!res.ok) {
			throw new Error(`Export fetch failed: ${res.status}`);
		}
		const json = (await res.json()) as ApiAnalysisResults;
		total = json.total;
		for (const row of json.data) {
			if (all.length >= hardCap) break;
			all.push(toCsvRow(row));
		}
		options?.onProgress?.(all.length, total);
		// Safety: backend ran out of rows before reported total.
		if (json.data.length < pageSize) break;
		offset += pageSize;
	}

	return all;
}

/* ---------- Hook ---------- */

async function fetchSingleResult(txHash: string): Promise<RiskAlert | null> {
	const res = await fetchWithAuth(
		`/api/analysis/results/${encodeURIComponent(txHash)}`,
	);
	if (res.status === 404) return null;
	if (!res.ok) {
		throw new Error(`Analysis result request failed: ${res.status}`);
	}
	const json = (await res.json()) as ApiAnalysisResult;
	console.log("Fetched single analysis result:", json);
	return toRiskAlert(json);
}

/** Single-alert detail, by tx hash. Returns `null` if unknown. */
export function useRiskAlert(txHash: string | undefined) {
	return useQuery({
		queryKey: ["analysis", "result", txHash],
		queryFn: () => fetchSingleResult(txHash!),
		enabled: !!txHash,
	});
}

export function useRiskAlerts(
	params: RiskAlertsParams,
	options?: { pollMs?: number },
) {
	// 15s default matches the stats polling cadence. The dashboard mounts
	// multiple `useRiskAlerts` instances (table + critical-alert banner)
	// each with its own query key, so faster polling multiplies request
	// volume against the backend rate limit. Callers that genuinely need
	// faster updates can override via `pollMs`.
	const pollMs = options?.pollMs ?? 15_000;
	// 0 (or negative) disables auto-refetch — used by Reports.
	const refetchInterval = pollMs > 0 ? pollMs : (false as const);

	return useQuery({
		queryKey: ["analysis", "results", params],
		queryFn: () => fetchRiskAlertsPage(params),
		refetchInterval,
		refetchIntervalInBackground: false,
		staleTime: pollMs > 0 ? pollMs / 2 : 30_000,
		// Keep the previous page visible while the new one loads — avoids the
		// loading flash when paging or changing filters.
		placeholderData: keepPreviousData,
	});
}
