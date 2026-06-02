import { useQuery } from "@tanstack/react-query";
import { fetchWithAuth, getNetwork } from "./fetch";

export type AnalysisStats = {
	total: number;
	critical_count: number;
	high_count: number;
	moderate_count: number;
	informational_count: number;
	avg_max_score: number | null;
	last_analyzed_at: string;
	// Ingested-but-unscored backlog, computed server-side as a single
	// like-for-like query (distinct tx_hashes with no score row). Prefer
	// this over `txStats.total_count - total`, which conflated duplicate
	// transaction rows and archived alerts into the figure.
	pending_count: number;
	per_class: Record<
		string,
		{
			scored_count: number;
			avg_score: number | null;
			max_score: number | null;
		}
	>;
};

export type TransactionStats = {
	total_count: number;
	total_volume: number;
	total_fees: number;
	avg_value: number;
	first_tx: string;
	last_tx: string;
};

export type TransactionThroughput = {
	window_minutes: number;
	count: number;
	tx_per_min: number;
};

async function fetchAnalysisStats(): Promise<AnalysisStats> {
	const res = await fetchWithAuth(
		`/api/analysis/stats?network=${getNetwork()}`,
	);
	if (!res.ok) throw new Error(`Analysis stats failed: ${res.status}`);
	return (await res.json()) as AnalysisStats;
}

async function fetchTransactionStats(): Promise<TransactionStats> {
	const res = await fetchWithAuth(
		`/api/transactions/stats/summary?network=${getNetwork()}`,
	);
	if (!res.ok) throw new Error(`Transaction stats failed: ${res.status}`);
	return (await res.json()) as TransactionStats;
}

export type AlertTimeseriesPoint = { date: string; count: number };

export type AlertTimeseries = {
	network: string;
	days: number;
	data: AlertTimeseriesPoint[];
};

async function fetchAlertTimeseries(days: number): Promise<AlertTimeseries> {
	const res = await fetchWithAuth(
		`/api/analysis/stats/timeseries?network=${getNetwork()}&days=${days}`,
	);
	if (!res.ok) throw new Error(`Alert timeseries failed: ${res.status}`);
	return (await res.json()) as AlertTimeseries;
}

async function fetchTransactionThroughput(
	windowMinutes: number,
): Promise<TransactionThroughput> {
	const res = await fetchWithAuth(
		`/api/transactions/stats/throughput?network=${getNetwork()}&window_minutes=${windowMinutes}`,
	);
	if (!res.ok) throw new Error(`Transaction throughput failed: ${res.status}`);
	return (await res.json()) as TransactionThroughput;
}

// Stats are aggregates that change slowly relative to the analysis engine
// cadence (30s). Polling them every 5s was 4x the engine's update rate, with
// no benefit. 15s keeps the UI fresh without burning rate-limit budget.
const POLL_MS = 15_000;

export function useAnalysisStats() {
	return useQuery({
		queryKey: ["analysis", "stats"],
		queryFn: fetchAnalysisStats,
		refetchInterval: POLL_MS,
		staleTime: POLL_MS / 2,
	});
}

export function useTransactionStats() {
	return useQuery({
		queryKey: ["transactions", "stats"],
		queryFn: fetchTransactionStats,
		refetchInterval: POLL_MS,
		staleTime: POLL_MS / 2,
	});
}

export function useAlertTimeseries(days = 14) {
	return useQuery({
		queryKey: ["analysis", "timeseries", days],
		queryFn: () => fetchAlertTimeseries(days),
		refetchInterval: POLL_MS,
		staleTime: POLL_MS / 2,
	});
}
/**
 * Recent throughput (tx/min). Window default = 5 min, smoothed enough to
 * not swing wildly on a single block but small enough to reflect "now".
 * Polls on the same cadence as the other KPIs.
 */
export function useTransactionThroughput(windowMinutes = 5) {
	return useQuery({
		queryKey: ["transactions", "throughput", windowMinutes],
		queryFn: () => fetchTransactionThroughput(windowMinutes),
		refetchInterval: POLL_MS,
		staleTime: POLL_MS / 2,
	});
}
