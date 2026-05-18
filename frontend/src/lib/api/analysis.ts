import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
	ATTACK_TYPES,
	type AttackType,
	type RiskAlert,
	type Severity,
} from "@/mocks/attacks";

/* ---------- Backend types (mirrors OpenAPI) ---------- */

type ApiRiskBand = "Low" | "Moderate" | "High" | "Critical";

type ApiAnalysisResult = {
	tx_hash: string;
	network: string;
	scores: Record<string, number>;
	max_score: number;
	max_class: string;
	risk_band: ApiRiskBand;
	sub_scores: Record<string, Record<string, number>>;
	analysis_version: string;
	analyzed_at: string; // ISO datetime
	fee: number | null; // lovelace
	output_count: number;
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

function formatAnalyzedAt(iso: string): string {
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return iso;
	const dd = String(d.getDate()).padStart(2, "0");
	const mm = String(d.getMonth() + 1).padStart(2, "0");
	const yyyy = d.getFullYear();
	const hh = String(d.getHours()).padStart(2, "0");
	const mi = String(d.getMinutes()).padStart(2, "0");
	return `${dd}.${mm}.${yyyy}, ${hh}:${mi}`;
}

/** Truncate a tx hash like Figma's `xxxx...xxxx` style. */
function shortHash(hash: string): string {
	if (hash.length <= 20) return hash;
	return `${hash.slice(0, 12)}...${hash.slice(-8)}`;
}

function toRiskAlert(r: ApiAnalysisResult): RiskAlert | null {
	const attackType = ATTACK_CLASS_BY_SNAKE[r.max_class];
	if (!attackType) return null;
	return {
		slug: r.tx_hash,
		id: shortHash(r.tx_hash),
		fullHash: r.tx_hash,
		date: formatAnalyzedAt(r.analyzed_at),
		attackType,
		severity: RISK_BAND_TO_SEVERITY[r.risk_band] ?? "LOW",
		riskScore: Math.round(r.max_score),
		feeAda: r.fee !== null ? r.fee / LOVELACE_PER_ADA : 0,
		outputs: r.output_count,
	};
}

/* ---------- Fetcher ---------- */

export type RiskAlertsParams = {
	page: number;
	pageSize: number;
	attackType?: AttackType;
	severity?: Severity;
	sort?: "score" | "date";
};

async function fetchRiskAlertsPage(
	p: RiskAlertsParams,
): Promise<RiskAlertsPage> {
	const qs = new URLSearchParams();
	qs.set("limit", String(p.pageSize));
	qs.set("offset", String(p.page * p.pageSize));
	qs.set("sort", p.sort ?? "date");
	// Only surface transactions that actually triggered an attack class.
	// Without this, the backend includes scored-but-clean transactions
	// (max_class="", max_score=0) which aren't really "alerts".
	qs.set("min_score", "1");
	if (p.attackType) qs.set("attack_class", SNAKE_BY_ATTACK_TYPE[p.attackType]);
	if (p.severity) qs.set("risk_band", SEVERITY_TO_RISK_BAND[p.severity]);
	const res = await fetch(`/api/analysis/results?${qs.toString()}`);
	if (!res.ok) {
		throw new Error(`Analysis results request failed: ${res.status}`);
	}
	const json = (await res.json()) as ApiAnalysisResults;
	return {
		rows: json.data
			.map(toRiskAlert)
			.filter((a): a is RiskAlert => a !== null),
		total: json.total,
	};
}

/* ---------- Hook ---------- */

export function useRiskAlerts(
	params: RiskAlertsParams,
	options?: { pollMs?: number },
) {
	const pollMs = options?.pollMs ?? 5_000;

	return useQuery({
		queryKey: ["analysis", "results", params],
		queryFn: () => fetchRiskAlertsPage(params),
		refetchInterval: pollMs,
		refetchIntervalInBackground: false,
		staleTime: pollMs / 2,
		// Keep the previous page visible while the new one loads — avoids the
		// loading flash when paging or changing filters.
		placeholderData: keepPreviousData,
	});
}
